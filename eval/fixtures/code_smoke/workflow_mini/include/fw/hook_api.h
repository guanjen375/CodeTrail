#ifndef FW_HOOK_API_H
#define FW_HOOK_API_H

typedef int (*hook_callback_t)(unsigned event_code);

void hook_register(hook_callback_t callback);
int hook_dispatch(unsigned event_code);

#endif
