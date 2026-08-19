#ifndef ISM_DISPATCHER_H
#define ISM_DISPATCHER_H

#include <stdint.h>

int dispatcher_drain_pending(
    uint32_t max_events,
    uint32_t *processed_out);

#endif /* ISM_DISPATCHER_H */
