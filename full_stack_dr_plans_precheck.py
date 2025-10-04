#!/usr/bin/env -S python3 -x
#
# Copyright (c) 2025, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v1.0 as shown at https://oss.oracle.com/licenses/upl.
#
# Example script written by Antoun Moubarak, Cloud Architecture Specialist

import argparse
import logging
import re
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Pattern

import oci
from oci.exceptions import ServiceError

# === Constants === #
DRPG_OCID_PATTERN = re.compile(r'^ocid1\.drprotectiongroup\.[^.]+\.[^.]+\.[^.]+$')
TOPIC_OCID_PATTERN = re.compile(r'^ocid1\.onstopic\.[^.]+\.[^.]+\.[^.]+$')
REGION_PATTERN = re.compile(r'^[a-z0-9]{2}-[a-z0-9]+-\d{1}$')

REGION_MAPPING = {
    'iad': 'us-ashburn-1',
    'phx': 'us-phoenix-1',
}


# === Enums === #
class DrPlanType(Enum):
    SWITCHOVER = "SWITCHOVER"
    FAILOVER = "FAILOVER"
    START_DRILL = "START_DRILL"
    STOP_DRILL = "STOP_DRILL"


# === Logging Configuration === #
class LevelFilter(logging.Filter):
    def __init__(self, level):
        super().__init__()
        self.level = level

    def filter(self, record):
        return record.levelno == self.level


def setup_logger(drpg_ocid: str, base_dir: Path):
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(exist_ok=True)

    timestamp = time.strftime("%Y%m%d%H%M%S")
    all_log = logs_dir / f"{drpg_ocid}.log"
    error_log = logs_dir / f"{drpg_ocid}_{timestamp}_error.log"

    logger = logging.getLogger("drpg_precheck")
    logger.setLevel(logging.DEBUG)

    #formatter = logging.Formatter(
    #    fmt='%(asctime)s %(levelname)-8s %(message)s',
    #    datefmt='%Y-%m-%d %H:%M:%S'
    #)
    formatter = logging.Formatter(
        fmt='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    handlers = []

    fh_all = logging.FileHandler(all_log)
    fh_all.setFormatter(formatter)
    fh_all.setLevel(logging.INFO)
    handlers.append(fh_all)

    fh_error = logging.FileHandler(error_log)
    fh_error.setFormatter(formatter)
    fh_error.setLevel(logging.ERROR)
    fh_error.addFilter(LevelFilter(logging.ERROR))
    handlers.append(fh_error)

    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    ch.setLevel(logging.INFO)
    handlers.append(ch)

    for h in logger.handlers[:]:
        logger.removeHandler(h)
    for h in handlers:
        logger.addHandler(h)

    return logger, error_log


# === Utility Functions === #
def is_valid_ocid(ocid: str, pattern: Pattern) -> bool:
    return bool(pattern.match(ocid))

def normalize_region(region: str) -> str:
    if REGION_PATTERN.match(region):
        return region
    return REGION_MAPPING.get(region)


def prepare_region_file(region: str, base_dir: Path, ocid: str) -> Path:
    region_file = base_dir / f"{ocid}.{time.strftime('%Y%m%d%H%M%S')}"
    region_file.write_text(f"[REGION]\nregion = {region}\n")
    return region_file


# === OCI Interactions === #
def get_drpg_details(drpg_ocid, client, logger):
    try:
        return client.get_dr_protection_group(drpg_ocid)
    except ServiceError as e:
        logger.error(f"Service error: {e.message}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
    return None


#def list_active_dr_plans(drpg_ocid, client, logger):
#    transitional_states = {"CREATING", "UPDATING", "DELETING"}
#    all_dr_plans = client.list_dr_plans(drpg_ocid)
#    for plan in all_dr_plans.data.items:
#        if plan.lifecycle_state in transitional_states:
#            logger.error(f"Plan {plan.display_name} is in {plan.lifecycle_state} state", exc_info=True)
#            sys.exit(1)
#    try:
#        return client.list_dr_plans(drpg_ocid, lifecycle_state="ACTIVE")
#    except Exception as e:
#        logger.error(f"Failed to list DR plans: {str(e)}", exc_info=True)
#    return None

def list_active_dr_plans(drpg_ocid, client, logger):
    transitional_states = {"CREATING", "UPDATING", "DELETING"}

    try:
        all_dr_plans = client.list_dr_plans(drpg_ocid)
        for plan in all_dr_plans.data.items:
            if plan.lifecycle_state in transitional_states:
                logger.error(f"Found transitional plan: {plan.display_name} in state {plan.lifecycle_state}")
                return plan.lifecycle_state

        # No transitional plans found, fetch ACTIVE ones
        active_plans = client.list_dr_plans(drpg_ocid, lifecycle_state="ACTIVE")
        logger.info(f"Found {len(active_plans.data.items)} active DR plans.")
        return active_plans.data.items  # Return list of active plans

    except Exception as e:
        logger.error(f"Failed to list DR plans: {str(e)}", exc_info=True)
        return None

def send_notification(signer, drpg_name, drpg_ocid, topic_ocid, error_log, base_dir: Path, logger):
    try:
        content = f"{drpg_name}: {drpg_ocid}\n\n" + error_log.read_text()
        topic_region_code = topic_ocid.split('.')[3]
        region = normalize_region(topic_region_code)

        if not region:
            logger.error("Unable to determine valid region for the topic.")
            sys.exit(1)

        region_file = prepare_region_file(region, base_dir, topic_ocid)
        config = oci.config.from_file(str(region_file), profile_name="REGION")
        client = oci.ons.NotificationDataPlaneClient(config=config, signer=signer)

        subject = f"FSDR Precheck Failed for {drpg_name} - {drpg_ocid}"

        client.publish_message(
            topic_id=topic_ocid,
            message_details=oci.ons.models.MessageDetails(
                body=content,
                title=subject
            )
        )

        region_file.unlink()

    except Exception as e:
        logger.error(f"Failed to send notification: {str(e)}", exc_info=True)


# === Main Logic === #
def run_prechecks(drpg_ocid: str, topic_ocid: str, base_dir: Path):
    logger, error_log = setup_logger(drpg_ocid, base_dir)

    if not is_valid_ocid(drpg_ocid, DRPG_OCID_PATTERN):
        logger.error(f"Invalid DRPG OCID format: {drpg_ocid}")
        sys.exit(1)

    if topic_ocid and not is_valid_ocid(topic_ocid, TOPIC_OCID_PATTERN):
        logger.error(f"Invalid Notification Topic OCID format: {topic_ocid}")
        sys.exit(1)

    region = normalize_region(drpg_ocid.split('.')[3])
    if not region:
        logger.error("Unable to determine region for DRPG.")
        sys.exit(1)

    region_file = prepare_region_file(region, base_dir, drpg_ocid)
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    config = oci.config.from_file(str(region_file), profile_name="REGION")
    dr_client = oci.disaster_recovery.DisasterRecoveryClient(config=config, signer=signer)

    drpg = get_drpg_details(drpg_ocid, dr_client, logger)
    if not drpg:
        region_file.unlink()
        if topic_ocid:
            send_notification(signer, "", drpg_ocid, topic_ocid, error_log, base_dir, logger)
        sys.exit(1)

    role = drpg.data.role
    peer_ocid = drpg.data.peer_id
    peer_region = drpg.data.peer_region

    if role == "UNCONFIGURED":
        logger.error("DRPG is unconfigured.")
        region_file.unlink()
        if topic_ocid:
            send_notification(signer, drpg.data.display_name, drpg_ocid, topic_ocid, error_log, base_dir, logger)
        sys.exit(1)

    if role == "PRIMARY":
        logger.warning("DRPG is PRIMARY, switching to PEER.")
        region_file.unlink()
        region = normalize_region(peer_region)
        if not region:
            logger.error("Unknown peer region.")
            region_file.unlink()
            sys.exit(1)
        region_file = prepare_region_file(region, base_dir, peer_ocid)
        config = oci.config.from_file(str(region_file), profile_name="REGION")
        dr_client = oci.disaster_recovery.DisasterRecoveryClient(config=config, signer=signer)
        drpg = get_drpg_details(peer_ocid, dr_client, logger)
        if not drpg:
            logger.error("Failed to get peer DRPG details.")
            region_file.unlink()
            if topic_ocid:
                send_notification(signer, "", peer_ocid, topic_ocid, error_log, base_dir, logger)
            sys.exit(1)

    standby_ocid = drpg.data.id
    standby_name = drpg.data.display_name
    standby_state = drpg.data.lifecycle_state

    logger.info(f"Standby DRPG: {standby_name} ({standby_ocid}) is {standby_state}")

    if standby_state != "ACTIVE":
        logger.error(f"Standby DRPG is not active.")
        region_file.unlink()
        if topic_ocid:
            send_notification(signer, standby_name, standby_ocid, topic_ocid, error_log, base_dir, logger)
        sys.exit(1)

    dr_plans = list_active_dr_plans(standby_ocid, dr_client, logger)

    if len(dr_plans) == 0:
        logger.error(f"No Active DR plans found in {standby_name}.")
        region_file.unlink()
        if topic_ocid:
            send_notification(signer, standby_name, standby_ocid, topic_ocid, error_log, base_dir, logger)
        sys.exit(1)
    
    if isinstance(dr_plans, str):
        logger.error(f"First transitional state found: {dr_plans}")
        region_file.unlink()
        if topic_ocid:
            send_notification(signer, standby_name, standby_ocid, topic_ocid, error_log, base_dir, logger)
        sys.exit(1)
    elif isinstance(dr_plans, list):
        for plan in dr_plans:
            plan_type = DrPlanType(plan.type)
            logger.info(f"Running precheck for {plan_type.value} plan: {plan.display_name}")

            if plan_type == DrPlanType.SWITCHOVER:
                options = oci.disaster_recovery.models.SwitchoverPrecheckExecutionOptionDetails()
            elif plan_type == DrPlanType.FAILOVER:
                options = oci.disaster_recovery.models.FailoverPrecheckExecutionOptionDetails()
            elif plan_type == DrPlanType.START_DRILL:
                options = oci.disaster_recovery.models.StartDrillPrecheckExecutionOptionDetails()
            elif plan_type == DrPlanType.STOP_DRILL:
                options = oci.disaster_recovery.models.StopDrillPrecheckExecutionOptionDetails()
            else:
                logger.error(f"Unknown plan type: {plan.type}")
                sys.exit(1)

            execution = dr_client.create_dr_plan_execution(
                oci.disaster_recovery.models.CreateDrPlanExecutionDetails(
                    plan_id=plan.id,
                    execution_options=options
                )
            )

            oci.wait_until(dr_client, dr_client.get_dr_plan_execution(execution.data.id), 'lifecycle_state', 'IN_PROGRESS')
            oci.wait_until(dr_client, get_drpg_details(standby_ocid, dr_client, logger), 'lifecycle_state', 'ACTIVE')
            final_status = dr_client.get_dr_plan_execution(execution.data.id)

            if final_status.data.lifecycle_state == "SUCCEEDED":
                logger.info(f"Precheck passed: {plan.display_name}")
            else:
                logger.error(f"Precheck failed: {plan.display_name}")

        if error_log.exists() and error_log.stat().st_size > 0 and topic_ocid:
            send_notification(signer, standby_name, standby_ocid, topic_ocid, error_log, base_dir, logger)

        if region_file.exists():
            region_file.unlink()
        if error_log.exists():
            error_log.unlink()

# === Entry Point === #
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run prechecks on all Active DR plans for a given OCI DRPG."
    )
    parser.add_argument("-id", "--drpg-ocid", required=True, help="DRPG OCID")
    parser.add_argument("-nf", "--ons-topic-ocid", help="Notification Topic OCID")
    args = parser.parse_args()

    current_dir = Path(__file__).parent
    run_prechecks(args.drpg_ocid, args.ons_topic_ocid, current_dir)

