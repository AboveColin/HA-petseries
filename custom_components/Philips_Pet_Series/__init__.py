from __future__ import annotations

import asyncio
import datetime as dt
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed

from petsseries import PetsSeriesClient
from petsseries.models import Event

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.BUTTON,
    Platform.CALENDAR,
]

SCAN_INTERVAL = timedelta(minutes=5)


class PhilipsPetsSeriesDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch data for Philips Pets Series sensors."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PetsSeriesClient,
        delay_between_calls: float = 1,
    ):
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self._client = client
        self.delay_between_calls = delay_between_calls  # Delay in seconds

    async def _async_update_data(self):
        """Fetch data from API."""
        try:
            homes = await self._client.get_homes()
            devices = []
            meals = []
            events_by_home_and_type = {}
            settings = {}
            event_types = Event.get_event_types()

            for home in homes:
                # Fetch devices for the home
                home_devices = await self._client.get_devices(home)
                devices.extend(home_devices)
                _LOGGER.debug(f"Fetched devices for home {home.id}")

                # Wait to respect rate limits
                await asyncio.sleep(self.delay_between_calls)

                for event_type in event_types:
                    # Fetch events for each event type separately
                    home_events = await self._client.events.get_events(
                        home,
                        from_date=dt.datetime(
                            2024, 1, 1, tzinfo=dt.timezone(dt.timedelta(hours=2))
                        ),
                        to_date=dt.datetime(
                            2100, 1, 1, tzinfo=dt.timezone(dt.timedelta(hours=2))
                        ),
                        types=str(event_type),
                    )
                    event_type_str = (
                        event_type
                        if isinstance(event_type, str)
                        else (
                            event_type.value
                            if hasattr(event_type, "value")
                            else str(event_type)
                        )
                    )

                    key = f"{home.id}_{event_type_str}"
                    events_by_home_and_type[key] = home_events
                    _LOGGER.debug(
                        f"Fetched events for home {home.id}, event type {event_type}"
                    )
                    _LOGGER.debug(f"Number of events: {len(home_events)}")

                    # Wait to respect rate limits
                    await asyncio.sleep(self.delay_between_calls)

                # Fetch settings for each device
                for device in home_devices:
                    device_settings = await self._client.get_settings(home, device.id)
                    settings[device.id] = device_settings
                    _LOGGER.debug(f"Fetched settings for device {device.id}")

                    # get Tuya status asynchronously
                    if self._client.tuya_client:
                        tuya_status = await asyncio.to_thread(
                            self._client.get_tuya_status
                        )
                        settings[device.id]["tuya_status"] = tuya_status
                    else:
                        settings[device.id]["tuya_status"] = None

                    # Wait to respect rate limits
                    await asyncio.sleep(self.delay_between_calls)
                # Fetch meals for each home
                home_meals = await self._client.meals.get_meals(home)
                meals.extend(home_meals)
                _LOGGER.debug(f"Fetched meals for home {home.id}")

                base_data = {}
                # Fetch Tuya status asynchronously
                # if tuya_credentials are provided
                if self._client.tuya_client:
                    tuya_status = await asyncio.to_thread(self._client.get_tuya_status)
                    base_data["tuya_status"] = tuya_status
                else:
                    base_data["tuya_status"] = None

                # Wait to respect rate limits
                await asyncio.sleep(self.delay_between_calls)

            return {
                "homes": homes,
                "devices": devices,
                "meals": meals,
                "events_by_home_and_type": events_by_home_and_type,
                "event_types": event_types,
                "settings": settings,
                "base_data": base_data,
            }
        except Exception as err:
            _LOGGER.exception("Error communicating with API: %s", err)
            raise UpdateFailed(f"Error communicating with API: {err}") from err


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Philips Pets Series from a config entry."""
    data = entry.data
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")

    # Extract Tuya credentials if provided
    tuya_credentials = None
    if all(
        key in data and data[key]
        for key in ("tuya_client_id", "tuya_ip", "tuya_local_key")
    ):
        tuya_credentials = {
            "client_id": data["tuya_client_id"],
            "ip": data["tuya_ip"],
            "local_key": data["tuya_local_key"],
            "version": data.get("tuya_version", 3.4),
        }

    client = PetsSeriesClient(
        access_token=access_token,
        refresh_token=refresh_token,
        tuya_credentials=tuya_credentials,
    )
    try:
        await client.initialize()
    except Exception as e:
        _LOGGER.error(f"Error initializing Philips Pets Series client: {e}")
        if "invalid_client" in str(e):
            raise ConfigEntryAuthFailed(
                "Invalid client credentials. Please re-authenticate."
            ) from e
        else:
            _LOGGER.error("Unexpected error during client initialization.")
            return False

    coordinator = PhilipsPetsSeriesDataUpdateCoordinator(
        hass,
        client,
        delay_between_calls=0.5,  # Adjust this value based on API rate limits
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
    }

    # Forward all platforms at once
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        client = hass.data[DOMAIN][entry.entry_id]["client"]
        await client.close()
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
