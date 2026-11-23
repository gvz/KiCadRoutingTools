"""Closed-loop place -> route -> check -> re-place optimizer.

Moves footprints with pcbnew, runs a project-specific routing pipeline, grades
the result with check_connected (+ optional kicad-cli DRC), and keeps a move only
when the score improves. Failed nets drive the move proposals; an optional LLM
proposer is consulted when the classical policy stalls.

Entry point: ``python3 -m place_route_loop --project <adapter.py> ...``
"""
