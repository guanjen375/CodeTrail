#include "dispatcher.h"
#include "event_queue.h"
#include "state_machine.h"

static void log_comm_event(const event_t *evt) {
    (void)evt;
}

int main(void) {
    event_queue_init();
    sm_init();
    sm_register_callback(EVT_COMM_RX, log_comm_event);
    for (;;) {
        uint32_t processed = 0u;
        dispatcher_drain_pending(4u, &processed);
        if (processed == 0u) {
            break;
        }
    }
    return 0;
}
