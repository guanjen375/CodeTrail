# Build rules for the mini firmware image.
CFLAGS  += -DRETRY_DEFAULT_ATTEMPTS=3
LDSCRIPT := config/link.ld

firmware.elf: $(OBJS)
	$(CC) $(CFLAGS) -T$(LDSCRIPT) -o $@ $^
