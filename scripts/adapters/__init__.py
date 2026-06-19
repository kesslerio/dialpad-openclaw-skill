"""Standalone context-command adapters for the Dialpad auto-responder.

Each adapter is invoked by webhook_server.py as a `DIALPAD_*_CONTEXT_COMMAND`
subprocess: the query arrives as a single final CLI arg and the adapter emits a
JSON object on stdout matching the contract in
`lookup_sales_crm_context` / `lookup_sales_calendar_context`.
"""
