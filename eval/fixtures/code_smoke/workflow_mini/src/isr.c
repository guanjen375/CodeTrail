#include "fw/alarm_queue.h"

void sensor_irq(unsigned status) {
    /* Assertion trace pending_count <= capacity appears after this ISR burst producer. */
    alarm_record_t record = {status, 2u};
    (void)alarm_queue_push(record);
}
