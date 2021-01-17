"""The Bosch Smart Home Controller integration."""
import asyncio
import logging

import voluptuous as vol
from boschshcpy import SHCSession, SHCUniversalSwitch
from boschshcpy.exceptions import (
    SHCAuthenticationError,
    SHCConnectionError,
    SHCmDNSError,
)
from homeassistant.components.zeroconf import async_get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DEVICE_ID,
    ATTR_ID,
    ATTR_NAME,
    CONF_HOST,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import (
    ATTR_BUTTON,
    ATTR_CLICK_TYPE,
    ATTR_LAST_TIME_TRIGGERED,
    CONF_SSL_CERTIFICATE,
    CONF_SSL_KEY,
    DOMAIN,
    EVENT_BOSCH_SHC_CLICK,
    EVENT_BOSCH_SHC_SCENARIO_TRIGGER,
    SERVICE_TRIGGER_SCENARIO,
    SUPPORTED_INPUTS_EVENTS_TYPES,
)

PLATFORMS = [
    "binary_sensor",
    "cover",
    "switch",
    "sensor",
    "climate",
    "alarm_control_panel",
    "light",
]

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Bosch SHC component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Bosch SHC from a config entry."""
    data = entry.data

    zeroconf = await async_get_instance(hass)
    try:
        session = await hass.async_add_executor_job(
            SHCSession,
            data[CONF_HOST],
            data[CONF_SSL_CERTIFICATE],
            data[CONF_SSL_KEY],
            False,
            zeroconf,
        )
    except SHCAuthenticationError as err:
        _LOGGER.warning("Unable to authenticate on Bosch Smart Home Controller API")
        raise ConfigEntryNotReady from err
    except (SHCConnectionError, SHCmDNSError) as err:
        raise ConfigEntryNotReady from err

    shc_info = session.information
    if shc_info.updateState.name == "UPDATE_AVAILABLE":
        _LOGGER.warning("Please check for software updates in the Bosch Smart Home App")

    hass.data[DOMAIN][entry.entry_id] = session

    device_registry = await dr.async_get_registry(hass)
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, dr.format_mac(shc_info.mac_address))},
        identifiers={(DOMAIN, shc_info.name)},
        manufacturer="Bosch",
        name=entry.title,
        model="SmartHomeController",
        sw_version=shc_info.version,
    )
    device_id = device_entry.id

    for component in PLATFORMS:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, component)
        )

    async def stop_polling(event):
        """Stop polling service."""
        await hass.async_add_executor_job(session.stop_polling)

    await hass.async_add_executor_job(session.start_polling)
    session.reset_connection_listener = hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STOP, stop_polling
    )

    @callback
    def _async_scenario_trigger(scenario_id, name, last_time_triggered):
        hass.bus.async_fire(
            EVENT_BOSCH_SHC_SCENARIO_TRIGGER,
            {
                ATTR_DEVICE_ID: device_id,
                ATTR_ID: scenario_id,
                ATTR_NAME: name,
                ATTR_LAST_TIME_TRIGGERED: last_time_triggered,
            },
        )

    session.subscribe_scenario_callback(_async_scenario_trigger)

    for switch_device in session.device_helper.universal_switches:
        event_listener = SwitchDeviceEventListener(hass, entry, switch_device)
        await event_listener.async_setup()

    register_services(hass, entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    session: SHCSession = hass.data[DOMAIN][entry.entry_id]
    session.unsubscribe_scenario_callback()

    if session.reset_connection_listener is not None:
        session.reset_connection_listener()
        session.reset_connection_listener = None
        await hass.async_add_executor_job(session.stop_polling)

    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, component)
                for component in PLATFORMS
            ]
        )
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


def register_services(hass, entry):
    """Register services for the component."""
    service_scenario_trigger_schema = vol.Schema(
        {
            vol.Required(ATTR_NAME): vol.All(
                cv.string, vol.In(hass.data[DOMAIN][entry.entry_id].scenario_names)
            )
        }
    )

    async def scenario_service_call(call):
        """SHC Scenario service call."""
        name = call.data[ATTR_NAME]
        for scenario in hass.data[DOMAIN][entry.entry_id].scenarios:
            if scenario.name == name:
                hass.async_add_executor_job(scenario.trigger)

    hass.services.async_register(
        DOMAIN,
        SERVICE_TRIGGER_SCENARIO,
        scenario_service_call,
        service_scenario_trigger_schema,
    )


class SwitchDeviceEventListener:
    """Event listener for a Switch device."""

    def __init__(self, hass, entry, device: SHCUniversalSwitch):
        """Initialize the Switch device event listener."""
        self.hass = hass
        self.entry = entry
        self._device = device
        self._service = None
        self.device_id = None

        for service in self._device.device_services:
            if service.id == "Keypad":
                self._service = service
                self._service.subscribe_callback(
                    self._device.id, self._async_input_events_handler
                )

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._handle_ha_stop)

    @callback
    def _async_input_events_handler(self):
        """Handle device input events."""
        event_type = self._device.eventtype.name

        if event_type in SUPPORTED_INPUTS_EVENTS_TYPES:
            self.hass.bus.async_fire(
                EVENT_BOSCH_SHC_CLICK,
                {
                    ATTR_DEVICE_ID: self.device_id,
                    ATTR_ID: self._device.id,
                    ATTR_NAME: self._device.name,
                    ATTR_LAST_TIME_TRIGGERED: self._device.eventtimestamp,
                    ATTR_BUTTON: self._device.keyname.name,
                    ATTR_CLICK_TYPE: self._device.eventtype.name,
                },
            )
        else:
            _LOGGER.warning(
                "Switch input event %s for device %s is not supported, please open issue",
                event_type,
                self._device.name,
            )

    async def async_setup(self):
        """Set up the listener."""

        device_registry = await dr.async_get_registry(self.hass)
        device_entry = device_registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            name=self._device.name,
            identifiers={(DOMAIN, self._device.id)},
            manufacturer=self._device.manufacturer,
            model=self._device.device_model,
            via_device=(DOMAIN, self._device.parent_device_id),
        )
        self.device_id = device_entry.id

    def shutdown(self):
        """Shutdown the listener."""
        self._service.unsubscribe_callback(self._device.id)

    @callback
    def _handle_ha_stop(self, _):
        """Handle Home Assistant stopping."""
        _LOGGER.debug("Stopping Switch event listener for %s", self._device.name)
        self.shutdown()
