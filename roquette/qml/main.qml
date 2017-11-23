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
import QtQuick.Window 2.2
import "." as Roquette

ApplicationWindow {
    SystemPalette { id: systemPalette; colorGroup: SystemPalette.Active }

    width: 700
    height: 700
    title: qsTr("Roquette")
    visible: true

    menuBar: Roquette.MenuBar { }

    Roquette.CollectionView { }
}