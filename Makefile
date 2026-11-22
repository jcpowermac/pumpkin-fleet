PYRAMID_DIR := $(dir $(lastword $(MAKEFILE_LIST)))
ROOT := $(abspath $(PYRAMID_DIR))
S := $(ROOT)/scripts/spike

.PHONY: spike download ignition create-vm wait verify template clean

spike: download ignition create-vm wait verify template

download:
	$(S)/10-download-fcos.sh

ignition:
	$(S)/20-build-ignition.sh

create-vm:
	$(S)/30-create-vm.sh

wait:
	$(S)/40-wait-rebase.sh

verify:
	$(S)/50-verify.sh

template:
	$(S)/60-make-template.sh

clean:
	rm -rf $(ROOT)/build/*
