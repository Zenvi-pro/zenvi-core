"""
Director Marketplace UI

Dialog for browsing, downloading, and installing directors.
"""

import os
import json
from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QListWidget, QListWidgetItem, QTextEdit, QLineEdit, QMessageBox,
    QFileDialog, QGroupBox, QSplitter
)
from classes.logger import log


class DirectorMarketplaceDialog(QDialog):
    """
    Dialog for browsing and installing directors from marketplace.

    Features:
    - Browse available directors
    - View director details
    - Install from file
    - Export directors for sharing
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Director Marketplace")
        self.resize(900, 600)

        self.setup_ui()
        self.load_directors()

    def setup_ui(self):
        """Setup the UI layout."""
        layout = QVBoxLayout()

        # Header
        header = QLabel("<h2>Director Marketplace</h2>")
        layout.addWidget(header)

        # Main splitter
        splitter = QSplitter(Qt.Horizontal)

        # Left panel: Director list
        left_panel = QGroupBox("Available Directors")
        left_layout = QVBoxLayout()

        # Search box
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search directors...")
        self.search_box.textChanged.connect(self.filter_directors)
        left_layout.addWidget(self.search_box)

        # Director list
        self.director_list = QListWidget()
        self.director_list.currentItemChanged.connect(self.on_director_selected)
        left_layout.addWidget(self.director_list)

        left_panel.setLayout(left_layout)
        splitter.addWidget(left_panel)

        # Right panel: Director details
        right_panel = QGroupBox("Director Details")
        right_layout = QVBoxLayout()

        self.details_text = QTextEdit()
        self.details_text.setReadOnly(True)
        self.details_text.setPlaceholderText("Select a director to view details")
        right_layout.addWidget(self.details_text)

        # Action buttons
        btn_layout = QHBoxLayout()
        self.btn_install = QPushButton("✓ Installed")
        self.btn_install.setEnabled(False)
        self.btn_install.clicked.connect(self.install_director)
        btn_layout.addWidget(self.btn_install)

        self.btn_export = QPushButton("📤 Export")
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self.export_director)
        btn_layout.addWidget(self.btn_export)

        right_layout.addLayout(btn_layout)
        right_panel.setLayout(right_layout)
        splitter.addWidget(right_panel)

        splitter.setSizes([300, 600])
        layout.addWidget(splitter)

        # Bottom buttons
        bottom_layout = QHBoxLayout()

        self.btn_import = QPushButton("📥 Import from File...")
        self.btn_import.clicked.connect(self.import_from_file)
        bottom_layout.addWidget(self.btn_import)

        bottom_layout.addStretch()

        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        bottom_layout.addWidget(self.btn_close)

        layout.addLayout(bottom_layout)

        self.setLayout(layout)

    def load_directors(self):
        """Load available directors."""
        try:
            from classes.ai_directors.director_loader import get_director_loader

            loader = get_director_loader()
            self.directors = loader.list_available_directors()

            self.director_list.clear()
            for director in self.directors:
                item = QListWidgetItem(f"{director.name}")
                item.setData(Qt.UserRole, director)
                self.director_list.addItem(item)

            log.info(f"Loaded {len(self.directors)} directors into marketplace")

        except Exception as e:
            log.error(f"Failed to load directors: {e}", exc_info=True)
            QMessageBox.warning(self, "Error", f"Failed to load directors: {e}")

    def filter_directors(self):
        """Filter directors based on search text."""
        search_text = self.search_box.text().lower()

        for i in range(self.director_list.count()):
            item = self.director_list.item(i)
            director = item.data(Qt.UserRole)

            # Search in name, description, tags
            matches = (
                search_text in director.name.lower() or
                search_text in director.metadata.description.lower() or
                any(search_text in tag.lower() for tag in director.metadata.tags)
            )

            item.setHidden(not matches)

    def on_director_selected(self, current, previous):
        """Handle director selection."""
        if not current:
            self.details_text.clear()
            self.btn_install.setEnabled(False)
            self.btn_export.setEnabled(False)
            return

        director = current.data(Qt.UserRole)

        # Display details
        details = f"""<h3>{director.name}</h3>
<p><b>Version:</b> {director.metadata.version}<br>
<b>Author:</b> {director.metadata.author}</p>

<p>{director.metadata.description}</p>

<p><b>Expertise Areas:</b><br>
{', '.join(director.personality.expertise_areas)}</p>

<p><b>Analysis Focus:</b><br>
{', '.join(director.personality.analysis_focus)}</p>

<p><b>Critique Style:</b> {director.personality.critique_style}</p>

<p><b>Tags:</b> {', '.join(director.metadata.tags)}</p>
"""

        self.details_text.setHtml(details)
        self.btn_install.setEnabled(False)  # Already installed (from list)
        self.btn_export.setEnabled(True)

    def install_director(self):
        """Install selected director (not yet implemented)."""
        QMessageBox.information(
            self,
            "Not Implemented",
            "Installing directors from remote marketplace is not yet implemented.\n\n"
            "Use 'Import from File' to install custom directors."
        )

    def export_director(self):
        """Export selected director to file."""
        current = self.director_list.currentItem()
        if not current:
            return

        director = current.data(Qt.UserRole)

        # Ask for save location
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Export Director",
            f"{director.id}.director",
            "Director Files (*.director)"
        )

        if not filename:
            return

        try:
            from classes.ai_directors.director_marketplace import get_marketplace

            marketplace = get_marketplace()
            success = marketplace.export_director(director.id, filename)

            if success:
                QMessageBox.information(
                    self,
                    "Success",
                    f"Director exported to:\n{filename}"
                )
                log.info(f"Exported director {director.id} to {filename}")
            else:
                QMessageBox.warning(
                    self,
                    "Error",
                    "Failed to export director. Check logs for details."
                )

        except Exception as e:
            log.error(f"Export failed: {e}", exc_info=True)
            QMessageBox.warning(self, "Error", f"Export failed: {e}")

    def import_from_file(self):
        """Import director from .director file."""
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Import Director",
            "",
            "Director Files (*.director)"
        )

        if not filename:
            return

        try:
            from classes.ai_directors.director_marketplace import get_marketplace

            marketplace = get_marketplace()
            success = marketplace.install_director_from_file(filename)

            if success:
                QMessageBox.information(
                    self,
                    "Success",
                    f"Director imported successfully!\n\nReload the directors panel to see the new director."
                )
                log.info(f"Imported director from {filename}")

                # Reload directors list
                self.load_directors()
            else:
                QMessageBox.warning(
                    self,
                    "Error",
                    "Failed to import director. Check logs for details."
                )

        except Exception as e:
            log.error(f"Import failed: {e}", exc_info=True)
            QMessageBox.warning(self, "Error", f"Import failed: {e}")


def show_marketplace_dialog(parent=None):
    """Show the marketplace dialog."""
    dialog = DirectorMarketplaceDialog(parent)
    dialog.exec_()
