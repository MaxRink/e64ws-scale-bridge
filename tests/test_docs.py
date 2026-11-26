"""The documentation must match the code it describes.

These tests are deliberately cheap and text-based: they catch the two defects
that were reported against the docs (a stale Python test count and "four
Xenia routes" where the installer ships five) and they keep every review item
listed with an explicit status in docs/REVIEW_FOLLOWUPS.md.  The items that
were previously dropped without a row -- ``--restore``, a rejected action and
the grinder's response payload -- are in ``REVIEW_ITEMS`` like any other, so
deleting their row leaves the suite red.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

REVIEW_ITEMS = (
    "token refresh",
    "tls pinning",
    "wss reconnect",
    "tare",
    "dropout",
    "redaction",
    # The items that used to be dropped silently, and the ones this round added.
    "https exemption",
    "--max-shot-ms",
    "--restore",
    "rejected action",
    # The response payload was the third item dropped without a row; it is
    # pinned by phrase here so deleting the admission from the redaction row
    # fails this suite instead of leaving it green.
    "response payload",
    "audit flush",
    "serving",
    "uninstall",
    "rotation",
    "partial config writes",
    "ble sample queue",
    "timeout shot",
    # Found by the review of #2 and by the 2026-11-26 sweep.  The placeholder and
    # the retention row are pinned by phrase because deleting either is exactly
    # the kind of silent drop this tuple exists to prevent: the first is a defect
    # nobody reproduces by hand, the second is a guarantee a reader would assume.
    "snapshot placeholder",
    "retention is a ceiling",
    "dependency ceilings",
    "python test count",
)

STATUS_MARKERS = (
    "**implemented**",
    "**partial**",
    "**unimplemented**",
    "**documented trade-off**",
)


def discovered_test_count() -> int:
    suite = unittest.defaultTestLoader.discover(
        str(REPO / "tests"), pattern="test_*.py", top_level_dir=str(REPO)
    )

    def count(item: unittest.TestSuite | unittest.TestCase) -> int:
        if isinstance(item, unittest.TestSuite):
            return sum(count(child) for child in item)
        return 1

    return count(suite)


def xenia_routes() -> list[str]:
    api = REPO / "ondevice" / "web" / "api" / "v2"
    return sorted(
        {path.parent.relative_to(api).as_posix() for path in api.rglob("index.php")}
    )


class DocumentationAgreementTests(unittest.TestCase):
    def test_python_test_count_matches_the_suite(self) -> None:
        total = discovered_test_count()
        self.assertGreater(total, 0)
        for name, statement in (
            ("README.md", f"{total} Python tests"),
            ("docs/ON_DEVICE_INSTALL.md", f"{total} tests"),
        ):
            text = (REPO / name).read_text(encoding="utf-8")
            self.assertIn(
                statement,
                text,
                f"{name} must state the real Python test count ({total})",
            )

    def test_xenia_route_count_matches_the_install(self) -> None:
        routes = xenia_routes()
        self.assertEqual(
            routes,
            ["brewratio", "ingest", "machine", "mako", "scripts/execute"],
        )
        # The installed lighttpd block must rewrite every shipped route.
        from ondevice.patch_lighttpd import PATCH

        for route in routes:
            self.assertIn(route, PATCH)
        # The on-device docs describe five routes: four Xenia-facing ones plus
        # the ESP32 ingest endpoint.
        for name in (
            "README.md",
            "docs/ON_DEVICE_INSTALL.md",
            "docs/ESP32_BLE_GATEWAY.md",
        ):
            text = (REPO / name).read_text(encoding="utf-8")
            self.assertNotIn("four Xenia", text, name)
            self.assertIn("five Xenia", text, name)

    def test_review_followups_lists_every_review_item_with_a_status(self) -> None:
        text = (REPO / "docs" / "REVIEW_FOLLOWUPS.md").read_text(encoding="utf-8")
        rows = [line for line in text.splitlines() if line.startswith("|")]
        for item in REVIEW_ITEMS:
            matches = [row for row in rows if item in row.lower()]
            self.assertTrue(matches, f"review item dropped from the table: {item}")
            self.assertTrue(
                any(marker in row for row in matches for marker in STATUS_MARKERS),
                f"review item has no explicit status: {item}",
            )
        self.assertIn("Nothing in this list is silently dropped", text)
        # The claim is only meaningful if the table also records what *was*
        # dropped, so the correction cannot be quietly deleted.
        self.assertIn("was **not** true before", text)


if __name__ == "__main__":
    unittest.main()
