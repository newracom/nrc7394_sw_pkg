#!/bin/bash

# Get the login ID
login_id=$(whoami)

# Determine the user's home directory
user_home=$(getent passwd "$login_id" | cut -d: -f6)

# Path to the VERSION-SDK file in the previous package
prev_pkg_version_file="$user_home/nrc_pkg/VERSION-SDK"

# Function to display an error message and exit
display_error() {
    echo "Error: $1"
    exit 1
}

# Function to read and parse version information
read_prev_pkg_version_info() {
    version_major=$(awk '/VERSION_MAJOR/ {print $2}' "$prev_pkg_version_file")
    version_minor=$(awk '/VERSION_MINOR/ {print $2}' "$prev_pkg_version_file")
    version_revision=$(awk '/VERSION_REVISION/ {print $2}' "$prev_pkg_version_file")
    version_rc=$(sed -n '4p' "$prev_pkg_version_file")
}

# Function to create a backup folder name
create_backup_folder() {
    if [ -n "$version_rc" ]; then
        backup_folder="nrc_pkg_v${version_major}_${version_minor}_${version_revision}_${version_rc}"
    else
        backup_folder="nrc_pkg_v${version_major}_${version_minor}_${version_revision}"
    fi
    echo "$backup_folder"
}

# Function to perform backup package
backup_pkg() {
    backup_folder_name=$(create_backup_folder)
    echo "$backup_folder_name"

    if [ -d "$user_home/$backup_folder_name" ]; then
        echo "Remove existing backup folder"
        rm -rf "$user_home/$backup_folder_name"
    fi

    if [ -d "$user_home/nrc_pkg" ]; then
        echo "Backup previous package"
        mv "$user_home/nrc_pkg" "$user_home/$backup_folder_name"
    fi
    sleep 1
}

# Function to perform copy new package
copy_pkg() {
    # Copy new package
    echo "Copy new package"
    echo "apply nrc_pkg"
    cp -r ./nrc_pkg/ "$user_home/nrc_pkg/"
}

# Function to check and build nrc.ko and cli_app
check_and_build_driver() {
    # Path to the script.start.py file
    script_file="./nrc_pkg/script/start.py"

    # Check if the script.start.py file exists
    if [ ! -f "$script_file" ]; then
        display_error "script.start.py file not found!"
    fi

    # Get the kernel version
    kernel_version=$(uname -r | cut -d- -f1)
    model_number=$(grep -oP 'model\s*=\s*\K\d+' "$script_file")
    echo "Model number: $model_number"
    echo "Kernel version: $kernel_version"

    # Check model and kernel version conditions
    if [ "$model_number" == "7292" ] && [ "$kernel_version" == "4.14.70" ]; then
        echo "Model is nrc7292 and kernel version is 4.14.70. No need to build nrc.ko and cli_app."
    elif [ "$model_number" == "7394" ] && [ "$kernel_version" == "5.10.17" ]; then
        echo "Model is nrc7394 and kernel version is 5.10.17. No need to build nrc.ko and cli_app."
    else
        # Build nrc.ko
        echo "Building nrc.ko"
        cd ../../src/nrc
        make clean
        make
        cp nrc.ko "$user_home/nrc_pkg/sw/driver/"

        # Build cli_app
        echo "Building cli_app"
        cd ../../src/cli_app
        make clean
        make
        cp cli_app "$user_home/nrc_pkg/script/"
    fi
}

# Function to change permissions
change_permissions() {
    echo "Change mode"
    cd "$user_home/nrc_pkg"
    sudo chmod 777 -R "$user_home/nrc_pkg"
    sleep 1
}

# Main script logic
if [ -f "$prev_pkg_version_file" ]; then
    read_prev_pkg_version_info
    backup_pkg
fi
copy_pkg
check_and_build_driver
change_permissions

echo "Done"

