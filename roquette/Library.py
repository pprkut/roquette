import os
import beets.library
from PyQt5.QtCore import *

class TrackItem(object):
    def __init__(self, data, parent=None):
        self.parentItem = parent
        self.libraryItem = data
        self.children = []

    def appendChild(self, item):
        self.children.append(item)

    def child(self, index):
        return self.children[index]

    def childCount(self):
        return len(self.children)

    def columnCount(self):
        return 1

    def data(self, index):
        if (self.libraryItem):
            return self.libraryItem.title

        return None

    def parent(self):
        return self.parentItem

    def row(self):
        if (self.parentItem):
            return self.parentItem.children.index(self)

        return 0

    def reset(self):
        self.children = []

class LibraryModel(QAbstractItemModel):
    def __init__(self, parent=None):
        super(LibraryModel, self).__init__(parent)
        self.rootItem = TrackItem(None, None)

        # Setup beets
        libpath = os.path.expanduser('~/data/beets.blb')
        self.library = beets.library.Library(libpath)

        self.setupModelData('')

    @pyqtSlot(str)
    def search(self, query):
        self.rootItem.reset()
        self.setupModelData(query)
        self.beginResetModel()
        self.endResetModel()

    def roleNames(self):
        roles = {
            Qt.UserRole + 1: b"TitleRole",
        }
        return roles

    def columnCount(self, parent):
        if parent.isValid():
            return parent.internalPointer().columnCount()
        else:
            return self.rootItem.columnCount()

    def data(self, index, role):
        if not index.isValid():
            return None

        item = index.internalPointer()

        return item.data(index.column())

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags

        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def headerData(self, section, orientation, role):
        return None

    def index(self, row, column, parent):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        if not parent.isValid():
            parentItem = self.rootItem
        else:
            parentItem = parent.internalPointer()

        childItem = parentItem.child(row)
        if childItem:
            return self.createIndex(row, column, childItem)
        else:
            return QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()

        childItem = index.internalPointer()
        parentItem = childItem.parent()

        if parentItem == self.rootItem:
            return QModelIndex()

        return self.createIndex(parentItem.row(), 0, parentItem)

    def rowCount(self, parent):
        if parent.column() > 0:
            return 0

        if not parent.isValid():
            parentItem = self.rootItem
        else:
            parentItem = parent.internalPointer()

        return parentItem.childCount()

    def setupModelData(self, query):
        for item in self.library.items(query):
            self.rootItem.appendChild(TrackItem(item, self.rootItem))
