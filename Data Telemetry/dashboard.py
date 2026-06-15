import sys
import serial
import serial.tools.list_ports
import pandas as pd
import re
from collections import deque
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QPushButton, QComboBox, QLabel, QStatusBar, QFileDialog, QCheckBox,
    QMessageBox, QLineEdit
)
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QFont

import pyqtgraph as pg

# Configure graphing behavior for real-time visualization
MAX_PLOT_POINTS = 300
pg.setConfigOption('background', 'w')
pg.setConfigOption('foreground', 'k')


# Thread class to handle asynchronous serial communication.
# This prevents the GUI from freezing while waiting for data from the hardware.
class SerialWorker(QThread):
    data_received = pyqtSignal(float, float, float)
    error_occurred = pyqtSignal(str)
    connection_successful = pyqtSignal()

    def __init__(self, port, baudrate=115200):
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self.serial_port = None
        self.is_running = False

    def run(self):
        """Main execution loop for the serial thread."""
        try:
            # Initialize serial connection with a timeout to prevent blocking indefinitely
            self.serial_port = serial.Serial(self.port, self.baudrate, timeout=2)
            self.is_running = True
            
            # Send 'P' (Ping) to verify device handshake before streaming
            self.serial_port.write(b'P')
            response = self.serial_port.readline().decode('utf-8').strip()
            if response == 'A':
                self.connection_successful.emit()
            else:
                raise serial.SerialException("Handshake failed. Device did not respond correctly.")

            # Continuously read and parse incoming data
            while self.is_running:
                if self.serial_port.in_waiting > 0:
                    try:
                        line = self.serial_port.readline().decode('utf-8').strip()
                        if line:
                            # Parse expected CSV-style format: millis,tds,temp
                            parts = line.split(',')
                            if len(parts) == 3:
                                millis, tds, temp = map(float, parts)
                                self.data_received.emit(millis, tds, temp)
                    except (UnicodeDecodeError, ValueError):
                        # Gracefully skip malformed packets
                        continue
        except serial.SerialException as e:
            self.error_occurred.emit(f"Serial Error: {e}")
        finally:
            if self.serial_port and self.serial_port.is_open:
                self.serial_port.close()

    def send_command(self, command):
        """Send specific control bytes to the hardware."""
        if self.serial_port and self.serial_port.is_open:
            self.serial_port.write(command.encode('utf-8'))

    def stop(self):
        """Halt streaming and shut down the thread."""
        if self.is_running:
            self.send_command('H')
        self.is_running = False
        self.wait(1000)


# Main GUI window handling layout and interactivity
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Water Quality Dashboard")
        self.setGeometry(100, 100, 1200, 800)
        
        # Internal data structures for logging and real-time plotting
        self.timestamps = []
        self.tds_data = []
        self.temp_data = []
        self.plot_timestamps = deque(maxlen=MAX_PLOT_POINTS)
        self.plot_tds_data = deque(maxlen=MAX_PLOT_POINTS)
        self.plot_temp_data = deque(maxlen=MAX_PLOT_POINTS)
        
        self.is_recording = False
        self.start_time = 0
        
        # Build UI structure
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QHBoxLayout(self.central_widget)
        
        self._create_control_panel()
        self._create_plot_panel()
        
        self.main_layout.addLayout(self.control_panel_layout, 1)
        self.main_layout.addWidget(self.plot_widget, 3)
        
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.update_status("Disconnected")
        
        self.serial_worker = None

    def _create_control_panel(self):
        """Initializes the control sidebar UI elements."""
        self.control_panel_layout = QVBoxLayout()

        # Serial port selection section
        self.port_label = QLabel("Select Serial Port:")
        self.port_combo = QComboBox()
        self.refresh_button = QPushButton("Refresh Ports")
        self.connect_button = QPushButton("Connect")
        connection_layout = QHBoxLayout()
        connection_layout.addWidget(self.port_combo)
        connection_layout.addWidget(self.refresh_button)
        self.control_panel_layout.addWidget(self.port_label)
        self.control_panel_layout.addLayout(connection_layout)
        self.control_panel_layout.addWidget(self.connect_button)
        self.control_panel_layout.addSpacing(20)

        # File session naming
        self.session_label = QLabel("Session Name (Optional):")
        self.session_name_input = QLineEdit()
        self.session_name_input.setPlaceholderText("e.g., Calibration_Test")
        self.control_panel_layout.addWidget(self.session_label)
        self.control_panel_layout.addWidget(self.session_name_input)
        
        # Data recording controls
        self.record_button = QPushButton("Start Recording")
        self.record_button.setEnabled(False)
        self.export_button = QPushButton("Export Data to CSV")
        self.export_button.setEnabled(False)
        self.clear_button = QPushButton("Clear Data")
        self.clear_button.setEnabled(False)
        self.control_panel_layout.addWidget(self.record_button)
        self.control_panel_layout.addWidget(self.export_button)
        self.control_panel_layout.addWidget(self.clear_button)
        self.control_panel_layout.addSpacing(20)

        # Dynamic readouts
        font = QFont(); font.setPointSize(16); font.setBold(True)
        self.tds_label = QLabel("TDS (ppm): --"); self.tds_label.setFont(font)
        self.temp_label = QLabel("Temp (°C): --"); self.temp_label.setFont(font)
        self.control_panel_layout.addWidget(self.tds_label)
        self.control_panel_layout.addWidget(self.temp_label)
        
        # Plotting options
        self.pause_plot_checkbox = QCheckBox("Pause Live Plot")
        self.pause_plot_checkbox.setEnabled(False)
        self.control_panel_layout.addWidget(self.pause_plot_checkbox)
        self.control_panel_layout.addStretch()

        # Wire up widget signals
        self.refresh_button.clicked.connect(self.populate_ports)
        self.connect_button.clicked.connect(self.toggle_connection)
        self.record_button.clicked.connect(self.toggle_recording)
        self.export_button.clicked.connect(self.export_data)
        self.clear_button.clicked.connect(self.clear_data)
        self.populate_ports()

    def _create_plot_panel(self):
        """Initializes the plotting canvas and dual-axis setup."""
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setLabel('bottom', 'Time (seconds)')
        self.plot_widget.getAxis('left').setLabel('TDS (ppm)', color='#0000FF')
        self.plot_widget.getAxis('right').setLabel('Temperature (°C)', color='#FF0000')
        self.plot_widget.showGrid(x=True, y=True)
        self.plot_widget.addLegend()
        
        # Setup TDS line on primary Y-axis
        self.tds_plot_line = self.plot_widget.plot(pen=pg.mkPen(color='#0000FF', width=2), name="TDS")
        
        # Configure secondary axis for Temperature
        self.plot_widget.scene().addItem(self.plot_widget.getAxis('right'))
        self.plot_widget.getAxis('right').linkToView(self.plot_widget.getViewBox())
        self.temp_plot_line = pg.PlotDataItem(pen=pg.mkPen(color='#FF0000', width=2), name="Temperature")
        self.plot_widget.getViewBox().addItem(self.temp_plot_line)

    def populate_ports(self):
        """Scans and updates the available serial ports."""
        self.port_combo.clear()
        for port in serial.tools.list_ports.comports():
            self.port_combo.addItem(port.device)
    
    def update_status(self, message):
        """Helper to show status text in the status bar."""
        self.status_bar.showMessage(message)

    def closeEvent(self, event):
        """Ensures the serial worker thread is cleaned up upon application exit."""
        if self.serial_worker:
            self.serial_worker.stop()
        event.accept()

    def toggle_connection(self):
        """Starts/Stops the serial background thread."""
        if self.serial_worker is None:
            port = self.port_combo.currentText()
            if not port: return
            
            self.connect_button.setText("Connecting...")
            self.serial_worker = SerialWorker(port)
            # Connect worker signals to UI update methods
            self.serial_worker.data_received.connect(self.update_data)
            self.serial_worker.error_occurred.connect(self.handle_serial_error)
            self.serial_worker.connection_successful.connect(self.on_connection_success)
            self.serial_worker.start()
        else:
            self.serial_worker.stop()

    def on_connection_success(self):
        """Update UI state after successful hardware handshake."""
        self.connect_button.setText("Disconnect")
        self.record_button.setEnabled(True)
        self.pause_plot_checkbox.setEnabled(True)

    def handle_serial_error(self, message):
        """Display critical serial errors to the user."""
        QMessageBox.critical(self, "Serial Error", message)
        if self.serial_worker: self.serial_worker.stop()

    def on_worker_finished(self):
        """Clean up references after thread completion."""
        self.serial_worker = None
        self.reset_ui_to_disconnected()

    def reset_ui_to_disconnected(self):
        """Reset buttons to their initial state upon disconnection."""
        self.connect_button.setText("Connect")
        self.record_button.setEnabled(False)
        self.pause_plot_checkbox.setEnabled(False)

    def toggle_recording(self):
        """Control recording state and send start/stop signals to the Arduino."""
        self.is_recording = not self.is_recording
        if self.is_recording:
            self.clear_data()
            self.record_button.setText("Stop Recording")
            self.serial_worker.send_command('S') # 'S' for Start
            self.start_time = datetime.now()
        else:
            self.record_button.setText("Start Recording")
            self.export_button.setEnabled(True)
            self.clear_button.setEnabled(True)
            self.serial_worker.send_command('H') # 'H' for Halt

    def update_data(self, millis, tds, temp):
        """Handles incoming data packets, updating labels and plot buffers."""
        self.tds_label.setText(f"TDS (ppm): {tds:.2f}")
        self.temp_label.setText(f"Temp (°C): {temp:.2f}")

        if self.is_recording:
            now = datetime.now()
            elapsed = (now - self.start_time).total_seconds()
            
            # Append to history buffers
            self.timestamps.append(now)
            self.tds_data.append(tds)
            self.temp_data.append(temp)
            
            # Update plot buffers
            self.plot_timestamps.append(elapsed)
            self.plot_tds_data.append(tds)
            self.plot_temp_data.append(temp)

            # Refresh graph if not paused
            if not self.pause_plot_checkbox.isChecked():
                self.tds_plot_line.setData(list(self.plot_timestamps), list(self.plot_tds_data))
                self.temp_plot_line.setData(list(self.plot_timestamps), list(self.plot_temp_data))

    def export_data(self):
        """Exports gathered data to a user-specified CSV file."""
        if not self.timestamps: return
            
        path, _ = QFileDialog.getSaveFileName(self, "Save CSV")
        if path:
            df = pd.DataFrame({
                'Timestamp': [ts.isoformat() for ts in self.timestamps],
                'TDS(ppm)': self.tds_data,
                'Temperature(C)': self.temp_data
            })
            df.to_csv(path, index=False)

    def clear_data(self):
        """Flushes buffers and resets the visual plot area."""
        self.timestamps.clear(); self.tds_data.clear(); self.temp_data.clear()
        self.plot_timestamps.clear(); self.plot_tds_data.clear(); self.plot_temp_data.clear()
        self.tds_plot_line.clear(); self.temp_plot_line.clear()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
