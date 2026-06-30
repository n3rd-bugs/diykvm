# DKMS-built u_serial.ko with a tunable RX/TX request queue depth (default 256).
# `all` (the default goal, run by DKMS even though it sets KERNELRELEASE) fetches+patches the source into
# THIS build dir, then delegates to kbuild (which uses obj-m). No KERNELRELEASE guard -- DKMS sets it, so a
# guard would skip the fetch+build (it did: "No rule to make target u_serial.o").
KVER  ?= $(shell uname -r)
QUEUE ?= 256
KDIR  := /lib/modules/$(KVER)/build
obj-m := u_serial.o

# The `u_serial.c u_serial.h:` rule below is one recipe for two targets; DKMS builds with `make -j`, which
# would otherwise run fetch-source.sh twice concurrently and the 2nd patch races/fails. Serialize our targets
# (this does NOT affect the parallelism of the kernel sub-make in `all`).
.NOTPARALLEL:

all: u_serial.c u_serial.h
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

u_serial.c u_serial.h:
	bash fetch-source.sh "$(KVER)" "$(QUEUE)"

clean:
	-$(MAKE) -C $(KDIR) M=$(CURDIR) clean
	-rm -f u_serial.c u_serial.h
