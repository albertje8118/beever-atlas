"""Convenience wrapper — delegates to the plugin implementation.

The actual fetch logic lives in ``plugins/chatgpt_copilot/chatgpt/fetch.py``.
This file exists so you can keep running ``python fetch_chatgpt.py`` from the
project root without changing your workflow.
"""
from plugins.chatgpt_copilot.chatgpt.fetch import main

if __name__ == "__main__":
    main()
