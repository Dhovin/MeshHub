import asyncio
import logging

logger = logging.getLogger("EventBus")

class EventBus:
    def __init__(self):
        self._listeners = {}

    def subscribe(self, event_name, listener):
        """
        Subscribes a listener function to an event.
        Returns a callable unsubscribe function.
        """
        if event_name not in self._listeners:
            self._listeners[event_name] = []
        self._listeners[event_name].append(listener)
        
        def unsubscribe():
            if event_name in self._listeners:
                if listener in self._listeners[event_name]:
                    self._listeners[event_name].remove(listener)
        return unsubscribe

    def publish(self, event_name, data=None):
        """
        Publishes an event to all subscribed listeners.
        Supports both sync and async callbacks.
        """
        if event_name not in self._listeners:
            return
        
        # Make a copy of list to prevent concurrent modification issues during loops
        for listener in list(self._listeners[event_name]):
            try:
                if asyncio.iscoroutinefunction(listener):
                    # Run async callbacks on the running loop
                    asyncio.create_task(self._safe_run_async(listener, event_name, data))
                else:
                    # Execute synchronous callbacks immediately
                    listener(data)
            except Exception as e:
                logger.error(f"Error executing listener for event '{event_name}': {e}", exc_info=True)

    async def _safe_run_async(self, callback, event_name, data):
        try:
            await callback(data)
        except Exception as e:
            logger.error(f"Error executing async listener for event '{event_name}': {e}", exc_info=True)
