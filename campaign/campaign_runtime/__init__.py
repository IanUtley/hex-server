"""The campaign driven by the campaign editor's rules.

``rules_loader`` builds a zone's rules exactly as the editor simulates them
(``catalog_data`` / ``records`` / ``offers`` are the shared data side). A zone
whose rules file says ``"runtime": "rules"`` is run by this package instead of
``campaign.py``'s hand-written logic.
"""
