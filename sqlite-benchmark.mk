# Compile the upstream amalgamation and CLI without a configure phase.
.PHONY: all
all: libsqlite3.a sqlite3

sqlite3.o: sqlite3.c sqlite3.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -c sqlite3.c -o $@

shell.o: shell.c sqlite3.h sqlite3ext.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -c shell.c -o $@

libsqlite3.a: sqlite3.o
	$(AR) rcs $@ $<
	$(RANLIB) $@

sqlite3: shell.o libsqlite3.a
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ shell.o libsqlite3.a -ldl -lpthread -lm
