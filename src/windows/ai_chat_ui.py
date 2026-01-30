import os
from PyQt5.QtCore import Qt, QDateTime, QThread, pyqtSignal, QObject
from PyQt5.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QPushButton, QLabel, QComboBox, QMessageBox
)
from PyQt5.QtGui import QFont, QColor, QTextCursor

from classes.logger import log
from classes.ai_chat_functionality import AIChat


class _ChatWorker(QObject):
    """Background worker to call AI chat without blocking UI."""

    finished = pyqtSignal()
    result = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, ai_chat: AIChat, user_text: str):
        super().__init__()
        self._ai_chat = ai_chat
        self._user_text = user_text

    def run(self):
        try:
            response = self._ai_chat.send_message(self._user_text)
            self.result.emit(response)
        except Exception as exc:  # surface all errors to UI thread
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class AIChatWindow(QDockWidget):
    # Main AI Chat dock widget
    
    def __init__(self, parent=None):
        super().__init__("AI Assistant", parent)
        self.setObjectName("AIChatWindow")
        
        # Make it closable so it appears in View menu
        self.setFeatures(
            QDockWidget.DockWidgetClosable |
            QDockWidget.DockWidgetMovable |
            QDockWidget.DockWidgetFloatable
        )
        
        self.ai_chat = AIChat()
        self.is_processing = False
        
        # Main widget
        main = QWidget()
        layout = QVBoxLayout()
        main.setLayout(layout)
        self.setWidget(main)
        
        # Model selector
        model_h = QHBoxLayout()
        model_h.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        self.model_combo.currentTextChanged.connect(self._model_changed)
        model_h.addWidget(self.model_combo)
        model_h.addStretch()
        layout.addLayout(model_h)
        
        # Load available models
        self._load_available_models_async()
        
        # Chat display
        self.chat_box = QTextEdit()
        self.chat_box.setReadOnly(True)
        self.chat_box.setStyleSheet("background-color: #2b2b2b; color: #e0e0e0; border: 1px solid #404040;")
        layout.addWidget(self.chat_box)
        
        # Input area
        input_h = QHBoxLayout()
        self.msg_input = QTextEdit()
        self.msg_input.setMaximumHeight(60)
        self.msg_input.setPlaceholderText("Type message... (Enter to send, Shift+Enter for newline)")
        self.msg_input.setStyleSheet("background-color: #3b3b3b; color: #e0e0e0; border: 1px solid #404040;")
        input_h.addWidget(self.msg_input)
        layout.addLayout(input_h)
        
        # Buttons
        btn_h = QHBoxLayout()
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send_message)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_chat)
        btn_h.addStretch()
        btn_h.addWidget(self.send_btn)
        btn_h.addWidget(self.clear_btn)
        layout.addLayout(btn_h)
        
        # Keyboard shortcut
        self.msg_input.keyPressEvent = self._key_press
        
        # Welcome message
        self._add_system_msg("Welcome to AI Assistant!")
        self.ai_chat.set_model(self.model_combo.currentText())
        
        self.setMinimumWidth(400)
        self.setMinimumHeight(400)
    
    def _key_press(self, event):
        if event.key() == Qt.Key_Return and event.modifiers() != Qt.ShiftModifier:
            self.send_message()
        else:
            QTextEdit.keyPressEvent(self.msg_input, event)
    
    def send_message(self):
        if self.is_processing:
            QMessageBox.warning(self, "Wait", "Processing previous message...")
            return
        
        text = self.msg_input.toPlainText().strip()
        if not text:
            return
        
        self.ai_chat.set_model(self.model_combo.currentText())
        self._add_user_msg(text)
        self.msg_input.clear()
        
        self.is_processing = True
        self.send_btn.setEnabled(False)
        self.send_btn.setText("Processing...")
        
        # Kick work to background thread to avoid blocking UI / causing crashes
        self._worker_thread = QThread(self)
        self._worker = _ChatWorker(self.ai_chat, text)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.result.connect(self._on_chat_result)
        self._worker.error.connect(self._on_chat_error)
        self._worker.finished.connect(self._on_chat_finished)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _on_chat_result(self, response: str):
        self._add_assistant_msg(response)

    def _on_chat_error(self, error_msg: str):
        log.error(f"AI chat error: {error_msg}")
        self._add_system_msg(f"Error: {error_msg}")

    def _on_chat_finished(self):
        self.is_processing = False
        self.send_btn.setEnabled(True)
        self.send_btn.setText("Send")
        self.msg_input.setFocus()

    def _model_changed(self, model_name: str):
        self.ai_chat.set_model(model_name)
    
    def _load_available_models_async(self):
        """Load available models asynchronously to avoid blocking UI."""
        # Start with loading message
        self.model_combo.addItem("Loading models...")
        
        # Load real models in background
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(500, self._load_available_models)
    
    def _load_available_models(self):
        """Load available models from API."""
        try:
            from utility.ai.model_utils import list_available_models
            models = list_available_models()
            
            # Clear loading message
            self.model_combo.clear()
            
            if models:
                self.model_combo.addItems(models)
                self.model_combo.setCurrentIndex(0)
                log.info(f"Loaded {len(models)} models from API")
            else:
                # No models available - user exceeded quota or API key issue
                self.model_combo.addItem("No models available (check API quota)")
                self.model_combo.setEnabled(False)
                log.warning("No models found from API - check quota and API key")
        except Exception as e:
            log.error(f"Failed to load models: {e}")
            self.model_combo.clear()
            self.model_combo.addItem("Error loading models")
            self.model_combo.setEnabled(False)
    
    def clear_chat(self):
        reply = QMessageBox.question(self, "Clear", "Clear chat?", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.ai_chat.clear_session()
            self.chat_box.clear()
            self._add_system_msg("Chat cleared.")
    
    def _add_user_msg(self, text):
        self._add_msg(text, "user", "#4fc3f7")
    
    def _add_assistant_msg(self, text):
        self._add_msg(text, "assistant", "#81c784")
    
    def _add_system_msg(self, text):
        self._add_msg(text, "system", "#ffb74d")
    
    def _add_msg(self, text, role, color):
        cursor = self.chat_box.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.chat_box.setTextCursor(cursor)
        
        time = QDateTime.currentDateTime().toString("hh:mm:ss")
        self.chat_box.setTextColor(QColor(color))
        self.chat_box.insertPlainText(f"[{time}] {role}: ")
        self.chat_box.setTextColor(QColor("#e0e0e0"))
        self.chat_box.insertPlainText(text + "\n\n")
        
        cursor = self.chat_box.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.chat_box.setTextCursor(cursor)
