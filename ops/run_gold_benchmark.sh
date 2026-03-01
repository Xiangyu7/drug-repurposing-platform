#!/bin/bash
export SIG_PRIORITY=archs4
export TIMEOUT_CROSS_KG_SIGNATURE=14400
export TIMEOUT_ORIGIN_KG_CTGOV=14400
export TIMEOUT_LLM_STEP6=7200
export TIMEOUT_LLM_STEP8=3600
cd /root/drug-repurposing-platform
exec bash ops/start.sh start --mode dual --list ops/disease_list_gold_benchmark.txt
