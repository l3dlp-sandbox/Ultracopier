# Standalone unit test for TransferThread::toFinalPath() (test/unit/finalpath_test.cpp).
# toFinalPath() is #ifdef Q_OS_WIN32, so this is cross-built with the MXE mingw toolchain and
# RUN ON THE REAL WINDOWS BOX by cases/finalpath_unit.py (never under wine).
#
# TransferThread.cpp is compiled directly: the test calls only its static string helper, but the
# whole translation unit has to link, so cpp11addition.cpp (stringreplaceAll) and
# DriveManagement.cpp (referenced by TransferThread's move/same-drive logic) come along.
QT       += core gui widgets
CONFIG   += console c++17
CONFIG   -= app_bundle
TEMPLATE  = app
TARGET    = finalpath_test

SPEC = $$PWD/../..
INCLUDEPATH += $$SPEC $$SPEC/../../..

# The engine hard-errors ("if windows, WIDESTRING need be enabled") without this: on Windows
# INTERNALTYPEPATH is std::wstring. Same gate as CopyEngine.pro / other-pro/ultracopier-core.pro.
win32 {
    LIBS += -ladvapi32
    DEFINES += WIDESTRING
}

SOURCES += $$PWD/finalpath_test.cpp \
           $$SPEC/TransferThread.cpp \
           $$SPEC/DriveManagement.cpp \
           $$SPEC/../../../cpp11addition.cpp
HEADERS += $$SPEC/TransferThread.h $$SPEC/DriveManagement.h

QMAKE_CXXFLAGS += -Wall -Wextra -Wconversion
