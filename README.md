# Full Stack Disaster Recovery - DR Plans Precheck Tool

This tool performs prechecks on all active Disaster Recovery (DR) plans in a given OCI Disaster Recovery Protection Group (DRPG) to ensure system readiness and reliability.

## Requirements

- Python 3.6+
- OCI Python SDK
- Instance Principal authentication (runs inside OCI compute)

## Usage

```bash
full_stack_dr_plans_precheck.py -id DRPG_OCID [-nf ONS_TOPIC_OCID]

arguments:
  -id DRPG_OCID, --drpg-ocid DRPG_OCID
                        DRPG OCID
  -nf ONS_TOPIC_OCID, --ons-topic-ocid ONS_TOPIC_OCID
                        Notification Topic OCID
```

## Notes

- Logs are saved in the logs/ directory
- Only active DR plans will be prechecked
- Automatically switches to the standby DRPG if the primary is passed
- If errors occur and --ons-topic-ocid is provided, a notification will be sent
