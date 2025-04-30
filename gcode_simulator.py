import zmq
import math
import time
import threading
from PySide6.QtWidgets import (QApplication, QMainWindow, QHBoxLayout, 
                              QVBoxLayout, QSlider, QWidget)
from PySide6.QtCore import Qt, QTimer, QThread
from PySide6.QtGui import QPainter, QPen, QPaintEvent

PORT = 5556
DEFAULT_VELOCITY = 25.0

class PositionDrawer(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.desiredX_norm = 0.5
        self.desiredY_norm = 0.5
        self.currentX_norm = 0.5
        self.currentY_norm = 0.5
    
    def paintEvent(self, event: QPaintEvent) -> None:
        squareSize = min(event.rect().width(), event.rect().height())
        qp = QPainter(self)
        
        # Draw boundary
        qp.drawRect(0, 0, squareSize - 1, squareSize - 1)

        # Draw desired position (black circle)
        desiredX = self.desiredX_norm * squareSize
        desiredY = (1 - self.desiredY_norm) * squareSize
        qp.setPen(QPen(Qt.GlobalColor.black, 1))
        qp.drawEllipse(int(desiredX) - 6, int(desiredY) - 6, 12, 12)

        # Draw current position (darkCyan dot)
        currentX = self.currentX_norm * squareSize
        currentY = (1 - self.currentY_norm) * squareSize
        qp.setPen(QPen(Qt.GlobalColor.darkCyan, 1))
        qp.setBrush(Qt.GlobalColor.darkCyan)
        qp.drawEllipse(int(currentX) - 4, int(currentY) - 4, 8, 8)

class GCodeDisplay(QMainWindow):
    axisList = ("X", "Y", "Z", "W")
    
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Scanner Simulator")
        self.setGeometry(600, 600, 510, 400)
        self.setFixedSize(510, 400)
        
        self.sliders = {}
        for axis in self.axisList:
            self.sliders[axis] = QSlider(Qt.Orientation.Horizontal if axis == "X" else Qt.Orientation.Vertical)
            self.sliders[axis].setMinimum(-30000)
            self.sliders[axis].setMaximum(30000)
            self.sliders[axis].setValue(0)

        self.drawPanelXY = PositionDrawer()
        
        layout = QHBoxLayout()
        layoutDraw = QVBoxLayout()
        layoutDraw.addWidget(self.drawPanelXY)
        layoutDraw.addWidget(self.sliders["X"])
        layout.addLayout(layoutDraw)
        layout.addWidget(self.sliders["Y"])
        layout.addSpacing(50)
        for ii in range(2, len(self.axisList)):
            layout.addWidget(self.sliders[self.axisList[ii]])

        self.desiredPos = {axis: 0.0 for axis in self.axisList}
        self.currentPos = {axis: 0.0 for axis in self.axisList}  # Explicitly track current position
        self.velocity = {axis: DEFAULT_VELOCITY for axis in self.axisList}
        self.ticksPerMm = {axis: 50.0 for axis in self.axisList}

        self._start_pos = {axis: 0.0 for axis in self.axisList}
        self._motion_lock = threading.Lock()
        self._global_start_time = 0.0
        self._global_duration = 0.0
        self._is_moving = False
        self._start_pos = {}

        mainWidget = QWidget()
        mainWidget.setLayout(layout)
        self.setCentralWidget(mainWidget)

        # Faster update rate for smoother animation
        self.displayTimer = QTimer()
        self.displayTimer.setInterval(10)
        self.displayTimer.timeout.connect(self.updatePositions)
        self.displayTimer.start()

    def updatePositions(self) -> None:
        if self._is_moving:
            now = time.time()
            elapsed = now - self._global_start_time
            
            if elapsed < self._global_duration:
                fraction = min(elapsed / self._global_duration, 1.0)
                for axis in self.axisList:
                    self.currentPos[axis] = (
                        self._start_pos[axis] +
                        (self.desiredPos[axis] - self._start_pos[axis]) * fraction
                    )
            else:
                self.currentPos = self.desiredPos.copy()
                self._is_moving = False
                
        # Update sliders and visual display
        for axis in self.axisList:
            mm = self.currentPos[axis]
            ticks = int(round(mm * self.ticksPerMm[axis]))
            self.sliders[axis].setValue(ticks)
            
        # Update display dots with normalized positions
        self.drawPanelXY.desiredX_norm = (self.desiredPos["X"] + 300) / 600
        self.drawPanelXY.desiredY_norm = (self.desiredPos["Y"] + 300) / 600
        self.drawPanelXY.currentX_norm = (self.currentPos["X"] + 300) / 600
        self.drawPanelXY.currentY_norm = (self.currentPos["Y"] + 300) / 600
        self.drawPanelXY.update()

    def getCurrentPosition(self) -> dict[str, float]:
        return self.currentPos.copy()
    
    def getEndstopMin(self) -> dict[str, float]:
        return {axis: -300.0 for axis in self.axisList}

    def getEndstopMax(self) -> dict[str, float]:
        return {axis: 300.0 for axis in self.axisList}

    def isMoving(self) -> bool:
        return self._is_moving
        
    def startMovement(self, targetPos, distanceOrDuration=None):
        with self._motion_lock:  # Add lock for thread safety
            self._start_pos = self.getCurrentPosition()

            distance = math.sqrt(sum(
                (targetPos[axis] - self._start_pos[axis]) ** 2 
                for axis in targetPos
            ))
            
            self._global_duration = distance / DEFAULT_VELOCITY
            self._global_start_time = time.time()
            self._is_moving = True
            self.desiredPos.update(targetPos)

    def syncMovement(self, start_pos: dict[str, float], target_pos: dict[str, float], duration: float, start_time: float):
        with self._motion_lock:
            self._start_pos = start_pos.copy()
            self.desiredPos = target_pos.copy()
            self._global_duration = duration
            self._global_start_time = start_time
            self._is_moving = True


class SerialHandler(QThread):
    gcode: GCodeDisplay
    _socket: zmq.Socket

    def __init__(self, gcodeWindow) -> None:
        super().__init__()
        self.gcode = gcodeWindow
        self.running = True
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.PAIR)
        self._lock = threading.Lock()

    def stop(self) -> None:
        self.running = False

    def run(self) -> None:
        try:
            self._socket.bind(f"tcp://*:{PORT}")
            while self.running:
                try:
                    if self._socket.poll(100):  # Add timeout to avoid blocking indefinitely
                        line = self._socket.recv_string()
                        if line and not line.isspace():
                            ret = self.serialLoop(line)
                            if ret:
                                self._socket.send_string(f"{ret}\n")
                    else:
                        # Small delay to prevent CPU hogging
                        time.sleep(0.001)
                except zmq.ZMQError:
                    time.sleep(0.1)  # Recover from ZMQ errors
        finally:
            self._socket.close()

    def serialLoop(self, line) -> str:
        line = line.replace("\n", "").replace("\r", "")
        args = list(line.strip().split())
        if not args:
            return ""
        try:
            with self._lock:
                match args[0]:
                    case "G00" | "G01" | "V00" | "A00":
                        axisDict = {}
                        for arg in args[1:]:
                            try:
                                axis = arg[0]
                                val = float(arg[1:].strip())
                                axisDict[axis] = val
                            except (ValueError, IndexError):
                                return f"Error: Command '{line}' not properly formatted."
                        for axis in axisDict:
                            if axis not in self.gcode.axisList:
                                return f"Error: Command '{line}' formatting: Axis '{axis}' does not exist."
                        match args[0]:
                            case "G00":  # Absolute movement
                                # Create dict of axis index names to values
                                target_pos = {axis: axisDict[axis] for axis in axisDict}
                                
                                # Calculate distance for duration
                                start_pos = self.gcode.getCurrentPosition()
                                distance = math.sqrt(sum(
                                    (target_pos.get(axis, start_pos[axis]) - start_pos[axis]) ** 2 
                                    for axis in self.gcode.axisList if axis in target_pos
                                ))
                                
                                # Calculate duration based on average velocity
                                avg_velocity = sum(self.gcode.velocity.values()) / len(self.gcode.velocity)
                                duration = distance / avg_velocity if avg_velocity > 0 else 0.5
                                
                                # Start the movement
                                self.gcode.startMovement(target_pos, duration)
                                return "ok"
                                
                            case "G01":  # Relative movement
                                start_pos = self.gcode.getCurrentPosition()
                                target_pos = {axis: start_pos[axis] + axisDict[axis] for axis in axisDict}
                                
                                # Calculate distance for duration
                                distance = math.sqrt(sum(
                                    (axisDict[axis]) ** 2 
                                    for axis in axisDict
                                ))
                                
                                # Calculate duration based on average velocity
                                avg_velocity = sum(self.gcode.velocity.values()) / len(self.gcode.velocity)  
                                duration = distance / avg_velocity if avg_velocity > 0 else 0.5
                                
                                # Start the movement
                                self.gcode.startMovement(target_pos, duration)
                                return "ok"
                                
                            case "V00":  # Set velocity
                                for axis, value in axisDict.items():
                                    idx = self.gcode.axisList.index(axis)
                                    self.gcode.velocity[axis] = value
                                return "ok"
                                
                            case "A00":  # Set acceleration (not implemented in this simulator)
                                return "ok"
                                
                    case "G00?":  # Get current position
                        pos = self.gcode.getCurrentPosition()
                        return f"{' '.join(f'{axis}{val}' for axis, val in pos.items())}"
                        
                    case "E00-?":  # Get minimum endstops
                        mins = self.gcode.getEndstopMin()
                        return f"{' '.join(f'{axis}{val}' for axis, val in mins.items())}"
                        
                    case "E00+?":  # Get maximum endstops
                        maxs = self.gcode.getEndstopMax()
                        return f"{' '.join(f'{axis}{val}' for axis, val in maxs.items())}"
                        
                    case "Status?":  # Get status
                        status = "Moving" if self.gcode.isMoving() else "Ready"
                        return status
                        
                    case _:  # Unknown command
                        return f"Error: Command '{line}' not recognized."
        except Exception as e:
            return f"Error: {e} while parsing command: '{line}'"

if __name__ == "__main__":
    app = QApplication([])
    gcodeWindow = GCodeDisplay()
    ser = SerialHandler(gcodeWindow)
    ser._simulator_window = gcodeWindow
    app.aboutToQuit.connect(ser.stop)
    app.aboutToQuit.connect(ser.wait)
    ser.start()
    gcodeWindow.show()
    app.exec()