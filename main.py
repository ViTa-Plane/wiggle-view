# version 0.2.0

import sys
from PySide6.QtWidgets import QApplication
from app.ui import MainWindow


if __name__ == "__main__":
    app = QApplication(sys.argv)

    # Check if a file path argument was passed via command line or file association
    initial_path = sys.argv[1] if len(sys.argv) > 1 else None

    window = MainWindow(initial_image_path=initial_path)
    window.show()

    sys.exit(app.exec())