"""Social layer (milestone 2): X mega-account / KOL watch and contract-address confirmation.

Social data is confirmation, never the trigger. Every row keeps the platform's own timestamp
(`created_at`) and our clock (`received_at`) apart so features stay as-of; every call goes through
the budget gate with a USD cost; nothing here ever posts anywhere. Milestone 7: the confirmation
layer is a cost-aware planner (`planner.py` for the keys, `xbudget.py` for the money): every dollar
can be traced to an observation it confirmed or refuted, and the reports show what a confirmation cost.
"""
