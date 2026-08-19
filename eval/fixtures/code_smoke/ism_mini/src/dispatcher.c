#include "dispatcher.h"
#include "event_queue.h"
#include "state_machine.h"

int dispatcher_drain_pending(
    uint32_t max_events,
    uint32_t *processed_out)
{
    event_t evt;
    uint32_t processed = 0u;
    while (processed < max_events && event_queue_pop(&evt) == 0) {
        sm_handle_event(&evt);
        processed++;
    }
    if (processed_out != 0) {
        *processed_out = processed;
    }
    return (processed == max_events) ? 1 : 0;
}
