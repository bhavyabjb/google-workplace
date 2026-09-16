# Marks `app.orchestrator` as a package - this is the orchestration layer the
# assignment is centrally about. Submodules:
#   intent_classifier.py  - NL query -> structured Intent (LLM call #1)
#   planner.py             - Intent -> ExecutionPlan (a small DAG of PlanNodes)
#   executor.py            - runs the DAG (parallel where possible), collects NodeResults
#   synthesizer.py         - NodeResults -> natural-language response (LLM call #2)
#   agents/                - per-service (gmail/gcal/gdrive) search/execute/get_context
