"""Local JSON protocol helpers for the Mahlkönig E64 WS.

This module only builds/parses messages. It does not contact the cloud and it
does not make a network connection by itself. The command inventory is based
on static analysis of the supplied HEM-HSG-HMI-S01 V01.16 build; a live device
may gate commands by token, role, development flag, or actionType.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


READ_ONLY_COMMANDS = (
    "RequestDriverConfig",
    "RequestDriverState",
    "RequestLogMessages",
    "RequestAdcConfig",
    "RequestConfigManagerSettings",
    "RequestNachineInfo",
)

SPECIAL_COMMANDS = (
    "RequestOnboarding",
    "WifiStaConfig",
    "WifiConfigChanged",
    "CustomSignal1",
    "CustomSignal2",
)

# Direct registry types. Actions share the top-level ``GrinderAction`` type
# and select a callback through ``data.actionType``.
DIRECT_COMMANDS = READ_ONLY_COMMANDS + (
    "RequestOnboarding",
    "WifiStaConfig",
    "WifiConfigChanged",
    "UpdateDriverConfig",
    "UpdateCpuConfig",
    "CustomSignal1",
    "CustomSignal2",
)

ACTION_SUFFIXES = (
    "Stop",
    "StartGrindManual",
    "StartGrindGbw",
    "StartGrindGbt",
    "StartCalibration",
    "StartTara",
    "SaveLog",
    "ResetLog",
    "SetFanTemperatureCtrlEnabled",
    "CalibrateFan1",
    "CalibrateFan2",
    "ToggleBacklightPower",
    "SetBacklightBrightness",
    "SetFan1Duty",
    "SetFan2Duty",
    "SetAgsaTargetDdValue",
    "StopAgsa",
    "CalibrateDdSensor",
    "ResetDriverConfig",
    "SetLanguage",
    "StartEnduranceTest",
    "StopEnduranceTest",
    "SetSetupWizardState",
    "AddBrewEvent",
    "EspFlashApp",
    "EspEraseFlash",
    "EspReset",
    "NozzleLightRunStandby",
    "NozzleLightStop",
    "RcTopLeftClick",
    "RcTopLeftLong",
    "RcTopLeftPress",
    "RcTopLeftRelease",
    "RcBottomLeftClick",
    "RcRotaryClick",
    "RcRotaryLong",
    "RcRotaryDouble",
    "RcRotary",
    "GbsCreateReport",
)

HAZARDOUS_DIRECT_COMMANDS = frozenset({"UpdateDriverConfig", "UpdateCpuConfig"})

# The binary exposes these as physical-control or maintenance operations. The
# guard is intentionally conservative; callers can override it explicitly.
HAZARDOUS_ACTIONS = frozenset(
    {
        "StartGrindManual",
        "StartGrindGbw",
        "StartGrindGbt",
        "StartCalibration",
        "StartTara",
        "CalibrateFan1",
        "CalibrateFan2",
        "SetFan1Duty",
        "SetFan2Duty",
        "SetAgsaTargetDdValue",
        "StopAgsa",
        "CalibrateDdSensor",
        "AddBrewEvent",
        "ResetDriverConfig",
        "UpdateDriverConfig",
        "UpdateCpuConfig",
        "SetSetupWizardState",
        "EspFlashApp",
        "EspEraseFlash",
        "EspReset",
        "StartEnduranceTest",
        "StopEnduranceTest",
        "RcTopLeftClick",
        "RcTopLeftLong",
        "RcTopLeftPress",
        "RcTopLeftRelease",
        "RcBottomLeftClick",
        "RcRotaryClick",
        "RcRotaryLong",
        "RcRotaryDouble",
        "RcRotary",
    }
)


@dataclass
class E64WSProtocol:
    """Build messages with a monotonic local message ID."""

    next_msg_id: int = 1

    def message(self, command_type: str, data: Any = None) -> dict[str, Any]:
        message: dict[str, Any] = {
            "type": command_type,
            "msgId": self.next_msg_id,
        }
        self.next_msg_id += 1
        if data is not None:
            message["data"] = data
        return message

    def request(self, command_type: str, data: Any = None) -> dict[str, Any]:
        return self.message(command_type, data)

    def action(
        self,
        suffix: str,
        data: Mapping[str, Any] | None = None,
        *,
        action_type: str | None = None,
    ) -> dict[str, Any]:
        if suffix not in ACTION_SUFFIXES:
            raise ValueError(f"unknown recovered action suffix: {suffix}")
        payload: dict[str, Any] = dict(data or {})
        if action_type is not None and action_type != suffix:
            raise ValueError(
                "action_type must equal the recovered registry action suffix"
            )
        payload["actionType"] = suffix
        return self.message("GrinderAction", payload)

    @staticmethod
    def expected_type(message: Mapping[str, Any]) -> str:
        command_type = str(message.get("type", ""))
        if command_type in {
            "RequestOnboarding",
            "WifiStaConfig",
            "WifiConfigChanged",
            "CustomSignal1",
            "CustomSignal2",
        }:
            return {
                "RequestOnboarding": "RequestOnboardingResult",
                "WifiStaConfig": "WifiStaConfig",
                "WifiConfigChanged": "WifiConfigChangedProcessed",
                "CustomSignal1": "CustomSignal1Result",
                "CustomSignal2": "CustomSignal2Result",
            }[command_type]
        if command_type == "GrinderAction":
            data = message.get("data", {})
            if isinstance(data, Mapping) and data.get("actionType") == "GbsCreateReport":
                return "GbsCreateReport"
            return "GrinderActionResult"
        return command_type + "Result"

    @staticmethod
    def matches_response(request: Mapping[str, Any], response: Mapping[str, Any]) -> bool:
        if response.get("type") != E64WSProtocol.expected_type(request):
            return False
        if "refId" not in response:
            if request.get("type") == "GrinderAction":
                data = request.get("data", {})
                return isinstance(data, Mapping) and data.get("actionType") == "GbsCreateReport"
            return request.get("type") in {
                "RequestOnboarding",
                "WifiStaConfig",
                "WifiConfigChanged",
            }
        return response.get("refId") == request.get("msgId")


def describe(message: Mapping[str, Any]) -> str:
    """Return a compact, token-free diagnostic description."""

    command_type = str(message.get("type", ""))
    msg_id = message.get("msgId", "?")
    return f"{command_type} msgId={msg_id}"
