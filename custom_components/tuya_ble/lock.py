"""The Tuya BLE lock integration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import asyncio

from homeassistant.components.lock import (
    LockEntity,
    LockEntityDescription,
    LockEntityFeature,
    LockState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice

from .const import DOMAIN
from .devices import TuyaBLECoordinator, TuyaBLEData, TuyaBLEEntity, TuyaBLEProductInfo

import logging

_LOGGER = logging.getLogger(__name__)


@dataclass
class TuyaBLELockMapping:
    """Mapping for Tuya BLE Lock."""
    
    lock_dp_id: int  # DP for controlling lock (automatic_lock)
    state_dp_id: int  # DP for reading lock state (lock_motor_state)
    description: LockEntityDescription | None = None


@dataclass
class TuyaBLECategoryLockMapping:
    """Mapping for Tuya BLE Lock by category."""
    
    products: dict[str, list[TuyaBLELockMapping]] | None = None


category_mapping: dict[str, TuyaBLECategoryLockMapping] = {
    "ms": TuyaBLECategoryLockMapping(
        products={
            "0qxp5u7s": [  # Smart Lock
                TuyaBLELockMapping(
                    lock_dp_id=33,  # automatic_lock
                    state_dp_id=47,  # lock_motor_state (read-only)
                    description=LockEntityDescription(
                        key="lock",
                        name="Lock",
                    ),
                ),
            ],
        },
    ),
}


def get_mapping_by_device(
    device: TuyaBLEDevice,
) -> list[TuyaBLELockMapping]:
    """Get lock mappings for device."""
    category = device.category
    product_id = device.product_id
    mappings: list[TuyaBLELockMapping] = []
    
    if category in category_mapping:
        category_map = category_mapping[category]
        if category_map.products and product_id in category_map.products:
            mappings.extend(category_map.products[product_id])
    
    return mappings


class TuyaBLELock(TuyaBLEEntity, LockEntity):
    """Representation of a Tuya BLE Lock."""
    
    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: TuyaBLECoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        mapping: TuyaBLELockMapping,
    ) -> None:
        """Initialize the lock."""
        description = mapping.description or LockEntityDescription(
            key="lock",
            name="Lock",
        )
        super().__init__(hass, coordinator, device, product, description)
        self._mapping = mapping
        self._target_state: bool | None = None  # Track what state we're trying to achieve
        self._operation_timeout_handle: asyncio.TimerHandle | None = None
        
        if mapping.description:
            self._attr_has_entity_name = True
            self._attr_unique_id = f"{device.device_id}_{mapping.description.key}"
        else:
            self._attr_unique_id = f"{device.device_id}_lock"
    
    @property
    def is_locked(self) -> bool | None:
        """Return true if the lock is locked."""
        datapoint = self._device.datapoints.get_or_create(
            self._mapping.state_dp_id,
            TuyaBLEDataPointType.DT_BOOL,
            False,
        )
        # Assuming True means locked, False means unlocked
        # Adjust if the logic is reversed for your device
        if datapoint.value is not None:
            return not bool(datapoint.value)
        return None
    
    @property
    def is_locking(self) -> bool:
        """Return true if the lock is locking."""
        if self._target_state is None:
            return False
        current_state = self.is_locked
        if current_state is None:
            return False
        # We're locking if target is locked (True) but current is unlocked (False)
        return self._target_state is True and current_state is False
    
    @property
    def is_unlocking(self) -> bool:
        """Return true if the lock is unlocking."""
        if self._target_state is None:
            return False
        current_state = self.is_locked
        if current_state is None:
            return False
        # We're unlocking if target is unlocked (False) but current is locked (True)
        return self._target_state is False and current_state is True
    
    def _clear_operation_timeout(self) -> None:
        """Clear the operation timeout if it exists."""
        if self._operation_timeout_handle:
            self._operation_timeout_handle.cancel()
            self._operation_timeout_handle = None
    
    def _set_operation_timeout(self) -> None:
        """Set a timeout to reset target state if operation doesn't complete."""
        self._clear_operation_timeout()
        loop = asyncio.get_event_loop()
        self._operation_timeout_handle = loop.call_later(
            30.0,  # 30 second timeout
            self._on_operation_timeout
        )
    
    def _on_operation_timeout(self) -> None:
        """Called when operation timeout expires."""
        _LOGGER.warning("Lock operation timeout for %s", self.entity_id)
        self._target_state = None
        self.async_write_ha_state()

    async def async_lock(self, **kwargs) -> None:
        """Lock the lock."""
        self._clear_operation_timeout()
        self._target_state = True
        self._set_operation_timeout()
        
        datapoint = self._device.datapoints.get_or_create(
            self._mapping.lock_dp_id,
            TuyaBLEDataPointType.DT_BOOL,
            True,
        )
        if datapoint:
            await datapoint.set_value(True)
        self.async_write_ha_state()
    
    async def async_unlock(self, **kwargs) -> None:
        """Unlock the lock."""
        self._clear_operation_timeout()
        self._target_state = False
        self._set_operation_timeout()
        
        datapoint = self._device.datapoints.get_or_create(
            self._mapping.lock_dp_id,
            TuyaBLEDataPointType.DT_BOOL,
            False,
        )
        if datapoint:
            await datapoint.set_value(False)
        self.async_write_ha_state()
    
    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Check if we've reached the target state
        current_state = self.is_locked
        if current_state is not None and self._target_state is not None:
            if current_state == self._target_state:
                # We've reached the target state
                self._clear_operation_timeout()
                self._target_state = None
        self.async_write_ha_state()
    
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tuya BLE lock platform."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    mappings = get_mapping_by_device(data.device)
    
    entities: list[TuyaBLELock] = []
    for mapping in mappings:
        entities.append(
            TuyaBLELock(
                hass,
                data.coordinator,
                data.device,
                data.product,
                mapping,
            )
        )
    
    async_add_entities(entities)