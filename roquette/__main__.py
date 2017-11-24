# -*- coding: utf-8 -*-
# This file is part of roquette.
# Copyright 2017, Heinz Wiesinger.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

import sys
import signal
from PyQt5.QtWidgets import QApplication
from PyQt5.QtQml import QQmlApplicationEngine
from roquette import Library

def main(args=None):
    # Create main app
    roquette = QApplication(sys.argv)

    signal.signal(signal.SIGINT, signal.SIG_DFL)

    engine = QQmlApplicationEngine()
    context = engine.rootContext()

    library = Library.LibraryModel()

    # Connect library to the QML application
    context.setContextProperty("LibraryModel", library)

    # Execute the Application and Exit
    engine.load('roquette/qml/main.qml')
    sys.exit(roquette.exec_())

# Main Function
if __name__ == '__main__':
    main()
