// This file is part of roquette.
// Copyright 2017, Heinz Wiesinger.
//
// Permission is hereby granted, free of charge, to any person obtaining
// a copy of this software and associated documentation files (the
// "Software"), to deal in the Software without restriction, including
// without limitation the rights to use, copy, modify, merge, publish,
// distribute, sublicense, and/or sell copies of the Software, and to
// permit persons to whom the Software is furnished to do so, subject to
// the following conditions:
//
// The above copyright notice and this permission notice shall be
// included in all copies or substantial portions of the Software.

import QtQuick 2.4
import QtQuick.Controls 1.4
import QtQuick.Layouts 1.2

ColumnLayout {
    width: parent.width
    height: parent.height

    Timer {
        id: searchDelay
        interval: 500
        onTriggered: LibraryModel.search(searchText.text)
    }

    ToolBar {
        Layout.fillWidth: true
        RowLayout {

            id: searchBar
            width: parent.width
            height: parent.height

            visible: true

            TextField {
                id: searchText
                visible: true
                property bool ignoreTextChange: false
                placeholderText: qsTr("Type beets query...")
                Layout.fillWidth: true
                onTextChanged: searchDelay.restart()
            }
        }
    }

    TreeView {
        id: collectionView
        Layout.fillWidth: true
        Layout.fillHeight: true
        visible: true

        model: LibraryModel

        TableViewColumn {
            title: "Name"
            role: "TitleRole"
        }
    }
}
