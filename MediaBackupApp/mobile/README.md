# Media Backup Mobile (iOS/Android) Setup Guide

This is a native mobile application designed to securely backup your photos and videos to your computer.

## 🚀 How to Run on your iPhone

Since I cannot deploy an App Store binary directly to your phone, we use **Expo Go**, the industry-standard developer tool for running native React Native code.

### 1. Install Expo Go
Download the **Expo Go** app from the iOS App Store.

### 2. Start the Mobile Project
On your computer, open a terminal in the `MediaBackupApp/mobile` folder and run:
```bash
npm install
npx expo start
```
*Note: Ensure your computer and iPhone are on the same Wi-Fi network.*

### 3. Scan the QR Code
1.  A large QR code will appear in your terminal.
2.  Open your iPhone's **Camera** app and scan the QR code.
3.  Tap "Open in Expo Go".
4.  The native app will load, automatically find your PC, and begin scanning your photo library.

## 📱 Features
- **Auto-Discovery**: Automatically finds your computer on the local network.
- **Native Sync**: High-speed access to your iOS Photo Library.
- **Differential Backup**: Only uploads new photos and skipping duplicates.
- **Background Support**: More reliable than a standard web browser.
