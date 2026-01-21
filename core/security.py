import win32api
import win32file
import os
import json
import base64
import time
import subprocess
import sys
from datetime import datetime
import nacl.signing
import nacl.encoding
import nacl.exceptions

# Master Public Key (Verified)
PUBLIC_KEY_HEX = "3273bd72e470437daf0e23d965406c5bb7c079bec9f59ca1e2072f37645f417a"

# Simulation Path (For Verification without USB)
SIMULATION_DRIVE_PATH = os.path.join(os.getcwd(), "VIRTUAL_USB")

class LicenseManager:
    def __init__(self, public_key_hex=PUBLIC_KEY_HEX):
        try:
            self.verify_key = nacl.signing.VerifyKey(public_key_hex, encoder=nacl.encoding.HexEncoder)
        except:
            self.verify_key = None

    def get_removable_drives(self):
        """Returns a list of root paths for all removable drives + simulation paths."""
        drives = []
        
        # 1. Real Removable Drives
        try:
            bitmask = win32api.GetLogicalDrives()
            for letter in range(26):
                if bitmask & (1 << letter):
                    drive_letter = chr(65 + letter) + ":\\"
                    if win32file.GetDriveType(drive_letter) == win32file.DRIVE_REMOVABLE:
                        drives.append(drive_letter)
        except:
            pass

        # 2. Simulation Drive for Testing
        if os.path.exists(SIMULATION_DRIVE_PATH):
            drives.append(SIMULATION_DRIVE_PATH)
            
        return drives

    def get_volume_serial(self, drive_path):
        """Returns the Volume Serial Number. For simulation path, uses a fixed debug ID."""
        if drive_path == SIMULATION_DRIVE_PATH:
            return "DEBUG_USB_001"
            
        try:
            info = win32api.GetVolumeInformation(drive_path)
            return hex(info[1] & 0xFFFFFFFF).upper().replace("0X", "")
        except:
            return None

    def verify_license_file(self, license_path, current_serial):
        """
        Verifies a signed license file.
        Returns (is_valid, message, metadata)
        """
        if not os.path.exists(license_path):
            return False, "License file not found", None

        try:
            with open(license_path, "rb") as f:
                signed_data = f.read()

            # Verify signature
            # The file should contain the raw signed message from nacl.signing.SigningKey.sign()
            verified_json = self.verify_key.verify(signed_data)
            data = json.loads(verified_json.decode('utf-8'))

            # 1. Check Hardware ID
            target_id = data.get("hardware_id")
            if target_id != current_serial:
                return False, f"Hardware ID Mismatch (Expected {target_id}, found {current_serial})", data

            # 2. Check Expiry
            expiry_str = data.get("expiry") # Format: YYYY-MM-DD
            if expiry_str:
                expiry_dt = datetime.strptime(expiry_str, "%Y-%m-%d")
                if datetime.now() > expiry_dt:
                    return False, f"License Expired on {expiry_str}", data

            return True, "Success", data

        except nacl.exceptions.BadSignatureError:
            return False, "Invalid Signature (Corrupted or Fraudulent License)", None
        except Exception as e:
            return False, f"Verification Error: {str(e)}", None

    def check_protection(self):
        """
        Main entry point for the app.
        Scans all USBs for a valid license.
        Returns (is_allowed, status_msg, hardware_ids_checked)
        """
        drives = self.get_removable_drives()
        checked_ids = []

        if not drives:
            return False, "No USB Dongle Detected", []

        for drive in drives:
            serial = self.get_volume_serial(drive)
            if not serial: continue
            
            checked_ids.append(f"{drive} ({serial})")
            license_path = os.path.join(drive, "license.dat")
            
            is_valid, msg, meta = self.verify_license_file(license_path, serial)
            if is_valid:
                return True, f"Welcome {meta.get('client_name', 'User')}", checked_ids

    def get_sentinel_devices(self):
        """Returns a list of connected SafeNet/Sentinel devices (InstanceIDs)."""
        sentinels = []
        try:
            # Powershell command to find SafeNet/Sentinel devices
            # We look for Manufacturer 'SafeNet' or FriendlyNames containing 'Sentinel'/'HASP'
            # CRITICAL: Must use -Status OK to only find currently connected devices
            cmd = [
                "powershell", "-NoProfile", "-Command",
                "Get-PnpDevice -Status OK | Where-Object { ($_.FriendlyName -match 'Sentinel') -or ($_.FriendlyName -match 'HASP') -or ($_.Manufacturer -match 'SafeNet') } | Select-Object InstanceId, FriendlyName | ConvertTo-Json"
            ]
            
            # Using Popen to capture output
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, startupinfo=startupinfo)
            out, err = p.communicate()
            
            if out.strip():
                try:
                    data = json.loads(out)
                    # ConvertTo-Json returns dict if single item, list if multiple
                    if isinstance(data, dict): data = [data]
                    for item in data:
                        # Clean ID: USB\VID_0529&PID_0001\5&22... -> Use the whole InstanceId as unique HWID for binding
                        sentinels.append((item.get("InstanceId"), item.get("FriendlyName")))
                except:
                    pass
        except Exception as e:
            print(f"Sentinel Scan Error: {e}")
            
        return sentinels

    def check_protection(self):
        """
        Main entry point for the app.
        STRICT MODE: Scans ONLY Sentinel / SafeNet Keys.
        RELIES ON HARDWARE PRESENCE ONLY (No license.dat required).
        Returns (is_allowed, status_msg, hardware_ids_checked)
        """
        checked_ids = []
        
        # 2. Check Sentinel / SafeNet Keys (Dongle Mode)
        # Scan for connected dongles
        sentinels = self.get_sentinel_devices()
        
        if sentinels:
             # If ANY SafeNet device is found, allow access.
             for instance_id, friendly_name in sentinels:
                 checked_ids.append(f"Dongle: {friendly_name} ({instance_id})")
             
             return True, "SafeNet Dongle Detected (Access Granted)", checked_ids
        else:
             return False, "未偵測到 SafeNet 加密鎖 (No SafeNet Dongle Found)", []

if __name__ == "__main__":
    # Test Run
    lm = LicenseManager()
    allowed, msg, ids = lm.check_protection()
    print(f"Allowed: {allowed}")
    print(f"Status: {msg}")
    print(f"IDs Checked: {ids}")
