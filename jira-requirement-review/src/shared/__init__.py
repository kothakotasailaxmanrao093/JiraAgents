"""Namespace only — see scripts/sync_shared.py for why this file is empty.

Required so the Aetherion packer detects ``shared`` as a package; dropped from
the deploy tarball, so nothing may be defined here. Import the modules:

    from shared import adf, attachments      # jira-requirement-review (flat root)
    from src.shared import adf, attachments  # jira-task-creation (src root)
"""
