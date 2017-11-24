import os
import beets.library
from PyQt5.QtCore import *

class LibraryItem(object):
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

    def data(self, index):
        return None

    def parent(self):
        return self.parentItem

    def row(self):
        if (self.parentItem):
            return self.parentItem.children.index(self)

        return 0

    def reset(self):
        self.children = []

class TrackItem(LibraryItem):
    def __init__(self, data, parent=None):
        super(TrackItem, self).__init__(data, parent)

    def data(self, index):
        if (self.libraryItem):
            if (index == 0):
                return self.libraryItem.artist
            elif (index == 1):
                return self.libraryItem.title
            else:
                return None

        return None

class ArtistItem(LibraryItem):
    def __init__(self, data, parent=None):
        super(ArtistItem, self).__init__(data, parent)

    def data(self, index):
        if (self.libraryItem):
            if (index == 0):
                return self.libraryItem
            else:
                return None

        return None

class LibraryModel(QAbstractItemModel):
    def __init__(self, parent=None):
        super(LibraryModel, self).__init__(parent)
        self.rootItem = LibraryItem(None, None)
        self.artists = {}

        # Setup beets
        libpath = os.path.expanduser('~/data/beets.blb')
        self.library = beets.library.Library(libpath)

        self.setupModelData('')

    @pyqtSlot(str)
    def search(self, query):
        self.rootItem.reset()
        self.artists = {}
        self.setupModelData(query)
        self.beginResetModel()
        self.endResetModel()

    def roleNames(self):
        roles = {
            0: b"ArtistRole",
            1: b"TitleRole",
        }
        return roles

    def columnCount(self, parent):
        return 2;

    def data(self, index, role):
        if not index.isValid():
            return None

        item = index.internalPointer()

        return item.data(role)

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags

        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

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
            if item.albumartist in self.artists:
                artist = self.artists.get(item.albumartist)
            else:
                artist = ArtistItem(item.albumartist, self.rootItem)
                self.artists[item.albumartist] = artist
                self.rootItem.appendChild(artist)

            artist.appendChild(TrackItem(item, artist))
