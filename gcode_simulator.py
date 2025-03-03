
import zmq
import math

from PySide6.QtWidgets import QApplication, QMainWindow, QHBoxLayout, QVBoxLayout, QSlider, QWidget
from PySide6.QtCore import Qt, QTimer, QThread
from PySide6.QtGui import QPainter, QPen, QPaintEvent


PORT = 5556


class PositionDrawer(QWidget):
    desiredX_norm: float
    desiredY_norm: float
    currentX_norm: float
    currentY_norm: float

    def __init__(self) -> None:
        super().__init__()

        self.desiredX_norm = 0.5
        self.desiredY_norm = 0.5
        self.currentX_norm = 0.5
        self.currentY_norm = 0.5

        self.show()
    
    def paintEvent(self, a0: QPaintEvent | None) -> None:
        if a0 is None:
            return
        
        squareSize = min(a0.rect().width(), a0.rect().height())

        qp = QPainter()
        qp.begin(self)
        
        qp.drawRect(0, 0, squareSize - 1, squareSize - 1)

        desiredX = self.desiredX_norm * squareSize
        desiredY = (1 - self.desiredY_norm) * squareSize
        currentX = self.currentX_norm * squareSize
        currentY = (1 - self.currentY_norm) * squareSize

        qp.setPen(QPen(Qt.GlobalColor.black, 1))
        qp.drawEllipse(int(desiredX) - 6, int(desiredY) - 6, 12, 12)
        qp.setBrush(Qt.GlobalColor.darkCyan)
        qp.drawEllipse(int(currentX) - 4, int(currentY) - 4, 8, 8)

        qp.end()

class GCodeDisplay(QMainWindow):
    axisList = ("X", "Y", "Z", "W")
    
    sliders: dict[str, QSlider]
    desiredPos: dict[str, float]
    ticksPerMm: dict[str, float]
    velocity: dict[str, float]

    drawPanelXY: PositionDrawer

    def __init__(self) -> None:
        super().__init__()

        self.setGeometry(100, 100, 510, 400)
        self.setFixedSize(510, 400)
        
        self.sliders = {}
        for axis in self.axisList:
            self.sliders[axis] = QSlider(Qt.Orientation.Horizontal if axis == "X" else Qt.Orientation.Vertical)
            self.sliders[axis].setMinimum(-10000)
            self.sliders[axis].setMaximum(10000)

        self.drawPanelXY = PositionDrawer()

        layout = QHBoxLayout()
        layoutDraw = QVBoxLayout()
        layoutDraw.addWidget(self.drawPanelXY)
        layoutDraw.addWidget(self.sliders[self.axisList[0]])
        layout.addLayout(layoutDraw)
        layout.addWidget(self.sliders[self.axisList[1]])
        layout.addSpacing(50)
        for ii in range(2, len(self.axisList)):
            layout.addWidget(self.sliders[self.axisList[ii]])

        self.sliders[self.axisList[0]].valueChanged.connect(self.drawPanelXY.repaint)
        self.sliders[self.axisList[1]].valueChanged.connect(self.drawPanelXY.repaint)

        self.desiredPos = {axis:0.0 for axis in self.axisList}
        self.velocity = {axis:25.0 for axis in self.axisList}
        self.ticksPerMm = {axis:100.0 for axis in self.axisList}

        mainWidget = QWidget()
        mainWidget.setLayout(layout)

        self.setCentralWidget(mainWidget)
        self.show()

        self.displayTimer = QTimer()
        self.displayTimer.setInterval(1000 // 60)
        self.displayTimer.timeout.connect(self.displayLoop)
        self.displayTimer.start()

    def displayLoop(self) -> None:
        for axis in self.axisList:
            diff_ticks = int(self.desiredPos[axis] * self.ticksPerMm[axis]) - self.sliders[axis].sliderPosition()
            maxMove_ticks = int(self.velocity[axis] / 60 * self.ticksPerMm[axis])
            move_ticks = int(math.copysign(min(abs(diff_ticks), maxMove_ticks), diff_ticks))

            if axis == self.axisList[0]:
                self.drawPanelXY.currentX_norm = (10000 + self.sliders[axis].sliderPosition() + move_ticks) / 20000
                self.drawPanelXY.desiredX_norm = (10000 + int(self.desiredPos[axis] * self.ticksPerMm[axis])) / 20000
            elif axis == self.axisList[1]:
                self.drawPanelXY.currentY_norm = (10000 + self.sliders[axis].sliderPosition() + move_ticks) / 20000
                self.drawPanelXY.desiredY_norm = (10000 + int(self.desiredPos[axis] * self.ticksPerMm[axis])) / 20000
            
            self.sliders[axis].setSliderPosition(self.sliders[axis].sliderPosition() + move_ticks)
        
    
    def getCurrentPosition(self) -> dict[str, float]:
        return {axis:self.sliders[axis].sliderPosition() / self.ticksPerMm[axis] for axis in self.axisList}
    
    def getEndstopMin(self) -> dict[str, float]:
        return {axis:self.sliders[axis].minimum() / self.ticksPerMm[axis] for axis in self.axisList}
    
    def getEndstopMax(self) -> dict[str, float]:
        return {axis:self.sliders[axis].maximum() / self.ticksPerMm[axis] for axis in self.axisList}
    
    def getStatusIsMoving(self) -> bool:
        return any(int(self.desiredPos[axis] * self.ticksPerMm[axis]) != self.sliders[axis].sliderPosition() for axis in self.axisList)
    
    def setVelocity(self, velDict: dict[str, float]) -> None:
        self.velocity.update(velDict)
        




class SerialHandler(QThread):
    gcode: GCodeDisplay
    _socket: zmq.Socket

    def __init__(self, gcodeWindow) -> None:
        super().__init__()
        self.gcode = gcodeWindow
        self.running = True

        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.PAIR)

    def stop(self) -> None:
        self.running = False

    def run(self) -> None:
        try:
            self._socket.bind(f"tcp://*:{PORT}")
            
            while self.running:
                line = self._socket.recv_string()
                if line and not line.isspace():
                    print(f"Received command: '{line}'")
                    ret = self.serialLoop(line)
                    if ret:
                        print(f"    Returned: '{ret}'")
                        self._socket.send_string(f"{ret}\n")
        finally:
            self._socket.close()

    def serialLoop(self, line) -> str:
        args = list(line.split())
        if not args:
            return ""
        
        try:
            match args[0]:
                case "G00" | "G01" | "V00" | "A00":
                    axisDict = {arg[0] : float(arg[1::]) for arg in args[1::]}
                    for axis in axisDict:
                        if axis not in self.gcode.axisList:
                            return f"Error: Command '{line}' formatting: Axis '{axis}' does not exist."
                    match args[0]:
                        case "G00":
                            self.gcode.desiredPos.update(axisDict)
                        case "G01":
                            for axis,offset in axisDict.items():
                                self.gcode.desiredPos[axis] += offset
                        case "V00":
                            self.gcode.velocity.update(axisDict)
                        case "A00":
                            pass
                    return "ok"
                case "G00?":
                    return f"{" ".join(f"{axis}{val}" for axis,val in self.gcode.getCurrentPosition().items())}"
                case "E00-?":
                    return f"{" ".join(f"{axis}{val}" for axis,val in self.gcode.getEndstopMin().items())}"
                case "E00+?":
                    return f"{" ".join(f"{axis}{val}" for axis,val in self.gcode.getEndstopMax().items())}"
                case "Status?":
                    return "Moving" if self.gcode.getStatusIsMoving() else "Ready"
                case _:
                    return f"Error: Command '{line}' not recognized."
        except:
            return f"Error: Command '{line}' not properly formatted."

            


if __name__ == "__main__":
    app = QApplication([])
    gcodeWindow = GCodeDisplay()
    gcodeWindow.setWindowTitle("Scanner Simulator")

    ser = SerialHandler(gcodeWindow)
    app.aboutToQuit.connect(ser.stop)
    app.aboutToQuit.connect(ser.wait)
    ser.start()
    
    app.exec()








