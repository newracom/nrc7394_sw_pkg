CC ?= gcc
RM ?= rm -f

TARGET = chmgrd

DEBUG  = -g
CFLAGS += -Wall -Wno-unused-parameter

all: $(TARGET)

$(TARGET): $(TARGET).c
	@echo "$@"
	@$(CC) $(DEBUG) $(CFLAGS) $^ -o $@

clean:
	@$(RM) -v $(TARGET)
