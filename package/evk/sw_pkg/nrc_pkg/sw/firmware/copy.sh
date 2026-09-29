#!/bin/bash

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# Check if at least 3 arguments are provided
if [ $# -lt 3 ]; then
    echo "Usage: $0 <version> <board_file> <use_eeprom_config> [variant]"
    echo "  variant: mcp   = MCP/ESL-capable image (Unified AP), nrc<ver>_cspi_mcp.bin"
    echo "           plain = 802.11ah-only image (default),      nrc<ver>_cspi.bin"
    exit 1
fi

VERSION=$1
BOARD_FILE=$2
USE_EEPROM_CONFIG=$3
# Two firmware images ship side by side: the MCP build (Unified AP, where the
# chip also runs the ESL/MCP beacon engine) and the plain 802.11ah build used
# by every other mode. Selecting the wrong one leaves the AP without MCP or
# wastes the MCP engine on a mode that never uses it.
VARIANT=${4:-plain}

# Auto-detect firmware path based on script location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIRMWARE_PATH="${SCRIPT_DIR}"

LIB_FIRMWARE_PATH="/lib/firmware"
UNIVERSAL_FILE="uni_s1g.bin"
SOURCE_FILE=""
if [ "$VARIANT" = "mcp" ]; then
    EEPROM_FILE="nrc${VERSION}_cspi_mcp_eeprom.bin"
    BINARY_FILE="nrc${VERSION}_cspi_mcp.bin"
else
    EEPROM_FILE="nrc${VERSION}_cspi_eeprom.bin"
    BINARY_FILE="nrc${VERSION}_cspi.bin"
fi

echo "[*] Firmware path: ${FIRMWARE_PATH} (variant: ${VARIANT})"

# Decide which file to use for uni_s1g.bin based on $3
if [ "$USE_EEPROM_CONFIG" -eq 1 ]; then
    # If $3 is 1, use the EEPROM file
    SOURCE_FILE="${EEPROM_FILE}"
    echo "[*] Using EEPROM config: ${SOURCE_FILE}"
else
    # If $3 is 0, use the normal binary file
    SOURCE_FILE="${BINARY_FILE}"
    echo "[*] Using normal binary: ${SOURCE_FILE}"
fi

# Check if the source file exists before copying
if [ ! -f "${FIRMWARE_PATH}/${SOURCE_FILE}" ]; then
    echo "Error: Source file ${FIRMWARE_PATH}/${SOURCE_FILE} does not exist."
    if [ "$VARIANT" = "mcp" ]; then
        echo "       The Unified AP needs the MCP firmware image; build it with"
        echo "       'make select target=nrc${VERSION}.cspi+mcp+<vendor> && make'"
        echo "       and ship it as nrc${VERSION}_cspi_mcp.bin. Deliberately not"
        echo "       falling back to the plain image: that would start the AP"
        echo "       without the MCP/ESL engine and fail silently later."
    fi
    echo "Available files:"
    ls -la "${FIRMWARE_PATH}"/*.bin 2>/dev/null || echo "No .bin files found"
    exit 2
fi

# Copy the selected source file to uni_s1g.bin
echo "[*] Creating ${UNIVERSAL_FILE} from ${SOURCE_FILE}"
cp "${FIRMWARE_PATH}/${SOURCE_FILE}" "${FIRMWARE_PATH}/${UNIVERSAL_FILE}"
if [ $? -ne 0 ]; then
    echo "Error: Failed to copy ${SOURCE_FILE} to ${UNIVERSAL_FILE}."
    exit 3
fi

# Carry the matching symbol table across too. The Web GUI reads uni_s1g.sym
# to name the addresses in a core dump or a stack dump, and the two must be
# the same build - taking the symbols from whichever image was selected here
# is what keeps them in step.
if [ -f "${FIRMWARE_PATH}/${SOURCE_FILE%.bin}.sym" ]; then
    cp "${FIRMWARE_PATH}/${SOURCE_FILE%.bin}.sym" \
       "${FIRMWARE_PATH}/${UNIVERSAL_FILE%.bin}.sym"
    echo "[*] Symbol table: ${UNIVERSAL_FILE%.bin}.sym"
else
    rm -f "${FIRMWARE_PATH}/${UNIVERSAL_FILE%.bin}.sym"
fi

# Check if the universal file exists before copying to /lib/firmware
if [ -f "${FIRMWARE_PATH}/${UNIVERSAL_FILE}" ]; then
    echo "[*] Copying ${UNIVERSAL_FILE} to ${LIB_FIRMWARE_PATH}"
    sudo cp "${FIRMWARE_PATH}/${UNIVERSAL_FILE}" "${LIB_FIRMWARE_PATH}"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to copy ${UNIVERSAL_FILE} to ${LIB_FIRMWARE_PATH}."
        exit 4
    fi
    echo "[*] ${UNIVERSAL_FILE} copied successfully"
else
    echo "Error: Universal file ${UNIVERSAL_FILE} does not exist."
    exit 5
fi

# Check if the board file exists before copying
if [ -f "${FIRMWARE_PATH}/${BOARD_FILE}" ]; then
    echo "[*] Copying ${BOARD_FILE} to ${LIB_FIRMWARE_PATH}"
    sudo cp "${FIRMWARE_PATH}/${BOARD_FILE}" "${LIB_FIRMWARE_PATH}"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to copy ${BOARD_FILE} to ${LIB_FIRMWARE_PATH}."
        exit 6
    fi
    echo "[*] ${BOARD_FILE} copied successfully"
else
    echo "Error: Board file ${FIRMWARE_PATH}/${BOARD_FILE} does not exist."
    echo "Available files:"
    ls -la "${FIRMWARE_PATH}"/*.dat 2>/dev/null || echo "No .dat files found"
    exit 7
fi

# List files in destination directory
echo "[*] Files in ${LIB_FIRMWARE_PATH}:"
ls -al "${LIB_FIRMWARE_PATH}"/uni_s1g* "${LIB_FIRMWARE_PATH}"/*.dat 2>/dev/null || echo "No firmware files found in ${LIB_FIRMWARE_PATH}"

echo "[*] Firmware copy completed successfully"
