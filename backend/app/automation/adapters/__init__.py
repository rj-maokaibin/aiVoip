"""Automation adapter package.

Keep package import lightweight so read-only providers (for example PBX health)
do not pull optional WEB transport dependencies such as httpx.
"""

__all__ = ["EntryAdapter", "EntryResult", "WebEntryAdapter"]


def __getattr__(name: str):
    if name in __all__:
        from app.automation.adapters.entries.web import EntryAdapter, EntryResult, WebEntryAdapter

        return {
            "EntryAdapter": EntryAdapter,
            "EntryResult": EntryResult,
            "WebEntryAdapter": WebEntryAdapter,
        }[name]
    raise AttributeError(name)
