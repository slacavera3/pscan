#-------------------------------------------------
#
# Project created by QtCreator 2020-03-03T17:14:41
#
#-------------------------------------------------

QT       += core gui
qtHaveModule(opengl): QT += opengl

greaterThan(QT_MAJOR_VERSION, 4): QT += widgets

TARGET = TUCamSample
TEMPLATE = app

# The following define makes your compiler emit warnings if you use
# any feature of Qt which as been marked as deprecated (the exact warnings
# depend on your compiler). Please consult the documentation of the
# deprecated API in order to know how to port your code away from it.
DEFINES += QT_DEPRECATED_WARNINGS

# You can also make your code fail to compile if you use deprecated APIs.
# In order to do so, uncomment the following line.
# You can also select to disable deprecated APIs only up to a certain version of Qt.
#DEFINES += QT_DISABLE_DEPRECATED_BEFORE=0x060000    # disables all the APIs deprecated before Qt 6.0.0

win32 {
    contains(QT_ARCH, i386) {
        # 32Bit
        LIBS += -L$$PWD/sdk/lib/x86 -lTUCam
    }else {
        # 64Bit
        LIBS += -L$$PWD/sdk/lib/x64 -lTUCam
    }
}

linux {

DEFINES += TUCAM_TARGETOS_IS_LINUX

LIBS += \
    -L/usr/lib  -lTUCam \
}

SOURCES += \
        main.cpp \
        mainwindow.cpp \
    caminformation.cpp \
    cammaincontrol.cpp \
    camobject.cpp \
    drawingglwidget.cpp \
    waittingthread.cpp \
    camroi.cpp \
    camimagesave.cpp \
    camimageadjustment.cpp \
    camtrigger.cpp \
    camoutputtrigger.cpp

HEADERS += \
        mainwindow.h \
    caminformation.h \
    cammaincontrol.h \
    camobject.h \
    drawingglwidget.h \
    waittingthread.h \
    camroi.h \
    camimagesave.h \
    camimageadjustment.h \
    camtrigger.h \
    camoutputtrigger.h

FORMS += \
        mainwindow.ui \
    caminformation.ui \
    cammaincontrol.ui \
    camroi.ui \
    camimagesave.ui \
    camimageadjustment.ui \
    camtrigger.ui \
    camoutputtrigger.ui
