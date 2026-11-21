"""Ansiroll test package."""

import os
import sys


def ansible_playbook_executable():
    # Match the Ansible installed for the interpreter compiling the playbook.
    local = os.path.join(os.path.dirname(sys.executable), "ansible-playbook")
    return local if os.path.exists(local) else "ansible-playbook"
