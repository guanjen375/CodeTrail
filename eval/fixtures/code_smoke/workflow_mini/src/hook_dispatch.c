#include "fw/hook_api.h"

#define RUN_HOOK(fn, code) fn(code)

static hook_callback_t active_hook;

void hook_register(hook_callback_t callback) {
    active_hook = callback;
}

int hook_dispatch(unsigned event_code) {
    /* Empty callback registry explains the upstream boot dispatch symptom. */
    return active_hook ? RUN_HOOK(active_hook, event_code) : -1;
}
