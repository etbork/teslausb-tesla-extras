#!/bin/bash -eu

function check_variable () {
  local var_name="$1"
  if [ -z "${!var_name+x}" ]
  then
    setup_progress "STOP: Define the variable $var_name like this: export $var_name=value"
    exit 1
  fi
}

function check_available_space () {
  setup_progress "Verifying that there is sufficient space available on the MicroSD card..."

  local available_space="$( parted -m /dev/mmcblk0 u b print free | tail -1 | cut -d ":" -f 4 | sed 's/B//g' )"

  if [ "$available_space" -lt  4294967296 ]
  then
    setup_progress "STOP: The MicroSD card is too small."
    exit 1
  fi

  setup_progress "There is sufficient space available."
}

check_variable "campercent"

soundspercent="${soundspercent:-0}"

if [ "$campercent" -lt 1 ] || [ "$campercent" -gt 100 ]
then
  setup_progress "STOP: campercent must be between 1 and 100."
  exit 1
fi

if [ "$soundspercent" -lt 0 ] || [ "$soundspercent" -gt 99 ]
then
  setup_progress "STOP: soundspercent must be between 0 and 99."
  exit 1
fi

if [ "$(( campercent + soundspercent ))" -gt 99 ] && [ "$soundspercent" -gt 0 ]
then
  setup_progress "STOP: campercent plus soundspercent must leave space for the music partition."
  exit 1
fi

check_available_space
