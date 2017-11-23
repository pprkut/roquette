import os
import beets.library
from PyQt5.QtCore import *

class Collection(QObject):
    def __init__(self):
        QObject.__init__(self)

        # Setup beets
        libpath = os.path.expanduser('~/data/beets.blb')
        self.library = beets.library.Library(libpath)

    @pyqtSlot(str, result=str)
    def search(self, query):
        result = ''
        for item in self.library.items(query):
            result += item.albumartist + " - " + item.album  + "/" + item.artist + " - " + item.title + "\n"

        return result
