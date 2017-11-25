import os
import beets.library
from PyQt5.QtCore import *

class LibraryItem(object):
    def __init__(self, data, parent=None):
        self.parentItem = parent
        self.libraryItem = data
        self.children = []
        self.map = {}

    def appendChild(self, data):
        if data.albumartist in self.map:
            artist = self.map.get(data.albumartist)
        else:
            artist = ArtistItem(data.albumartist, self)
            self.map[data.albumartist] = artist
            self.children.append(artist)

        artist.appendChild(data)

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
        self.map = {}

class TrackItem(LibraryItem):
    def __init__(self, data, parent=None):
        super(TrackItem, self).__init__(data, parent)

    def appendChild(self, data):
        return None

    def data(self, index):
        if (self.libraryItem):
            if (index == 0):
                return '{:02d}. {} - {}'.format(self.libraryItem.track, self.libraryItem.artist, self.libraryItem.title)
            else:
                return None

        return None

class MediumItem(LibraryItem):
    def __init__(self, data, parent=None):
        super(MediumItem, self).__init__(data, parent)

    def appendChild(self, data):
        self.children.append(TrackItem(data, self))

    def data(self, index):
        if (self.libraryItem):
            if (index == 0):
                return self.libraryItem
            else:
                return None

        return None

class AlbumItem(LibraryItem):
    def __init__(self, data, parent=None):
        super(AlbumItem, self).__init__(data, parent)

    def appendChild(self, data):
        if data.disctotal == 1:
            self.children.append(TrackItem(data, self))
        else:
            if data.disc in self.map:
                medium = self.map.get(data.disc)
            else:
                if data.disctitle:
                    text = 'Medium {} ({}) - {}'.format(data.disc, data.media, data.disctitle)
                else:
                    text = 'Medium {} ({})'.format(data.disc, data.media)

                medium = MediumItem(text, self)
                self.map[data.disc] = medium
                self.children.append(medium)

            medium.appendChild(data)

    def data(self, index):
        if (self.libraryItem):
            if (index == 0):
                return self.libraryItem
            else:
                return None

        return None

class ArtistItem(LibraryItem):
    def __init__(self, data, parent=None):
        super(ArtistItem, self).__init__(data, parent)

    def appendChild(self, data):
        if data.album_id in self.map:
            album = self.map.get(data.album_id)
        else:
            if data.albumdisambig:
                text = '{} - {} ({})'.format(data.year, data.album, data.albumdisambig)
            else:
                text = '{} - {}'.format(data.year, data.album)

            album = AlbumItem(text, self)
            self.map[data.album_id] = album
            self.children.append(album)

        album.appendChild(data)

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
            0: b"TitleRole",
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
            self.rootItem.appendChild(item)
