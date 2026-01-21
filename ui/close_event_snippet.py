def closeEvent(self, event):
    try:
        if hasattr(self, 'player') and self.player:
            self.player.shutdown()
        if hasattr(self, 'settings'):
            self.save_settings()
    except Exception as e:
        print(f"Error during shutdown: {e}")
    event.accept()

if __name__ == "__main__":
    pass
