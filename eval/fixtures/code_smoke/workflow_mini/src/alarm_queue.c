#include "fw/alarm_queue.h"

static alarm_record_t queue_slots[4];
static unsigned pending_count;

int alarm_queue_push(alarm_record_t record) {
    /* Queue overflow keeps the oldest alarm; pending_count must stay within capacity. */
    if (pending_count >= 4u) {
        return -1;
    }
    queue_slots[pending_count++] = record;
    return 0;
}

unsigned alarm_queue_pending(void) {
    return pending_count;
}
