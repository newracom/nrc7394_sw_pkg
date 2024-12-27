#!/bin/bash

# Check if at least 3 arguments are provided
if [ $# -lt 3 ]; then
    echo "Usage: $0 <version> <board_file> <use_eeprom_config>"
    exit 1
fi

VERSION=$1
BOARD_FILE=$2
USE_EEPROM_CONFIG=$3

FIRMWARE_PATH="/home/pi/nrc_pkg/sw/firmware"
LIB_FIRMWARE_PATH="/lib/firmware"
UNIVERSAL_FILE="uni_s1g.bin"
SOURCE_FILE=""
EEPROM_FILE="nrc${VERSION}_cspi_eeprom.bin"
BINARY_FILE="nrc${VERSION}_cspi.bin"

# Decide which file to use for uni_s1g.bin based on $3
if [ "$USE_EEPROM_CONFIG" -eq 1 ]; then
    # If $3 is 1, use the EEPROM file
    SOURCE_FILE="${EEPROM_FILE}"
else
    # If $3 is 0, use the normal binary file
    SOURCE_FILE="${BINARY_FILE}"
fi

# Check if the source file exists before copying
if [ ! -f "${FIRMWARE_PATH}/${SOURCE_FILE}" ]; then
    echo "Error: Source file ${SOURCE_FILE} does not exist."
    exit 2
fi

# Copy the selected source file to uni_s1g.bin
cp "${FIRMWARE_PATH}/${SOURCE_FILE}" "${FIRMWARE_PATH}/${UNIVERSAL_FILE}"
if [ $? -ne 0 ]; then
    echo "Error: Failed to copy ${SOURCE_FILE} to ${UNIVERSAL_FILE}."
    exit 3
fi

# Check if the universal file exists before copying to /lib/firmware
if [ -f "${FIRMWARE_PATH}/${UNIVERSAL_FILE}" ]; then
    sudo cp "${FIRMWARE_PATH}/${UNIVERSAL_FILE}" "${LIB_FIRMWARE_PATH}"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to copy ${UNIVERSAL_FILE} to ${LIB_FIRMWARE_PATH}."
        exit 4
    fi
else
    echo "Error: Universal file ${UNIVERSAL_FILE} does not exist."
    exit 5
fi

# Check if the board file exists before copying
if [ -f "${FIRMWARE_PATH}/${BOARD_FILE}" ]; then
    sudo cp "${FIRMWARE_PATH}/${BOARD_FILE}" "${LIB_FIRMWARE_PATH}"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to copy ${BOARD_FILE} to ${LIB_FIRMWARE_PATH}."
        exit 6
    fi
else
    echo "Error: Board file ${BOARD_FILE} does not exist."
    exit 7
fi

# List files in source and destination directories
ls -al "${FIRMWARE_PATH}"
ls -al "${LIB_FIRMWARE_PATH}/uni_s1g*"