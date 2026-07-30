#include "mainwindow.h"

#include <QApplication>
#include <QDesktopWidget>

int main(int argc, char *argv[])
{
    QApplication a(argc, argv);

    QDesktopWidget *desktopWidget = QApplication::desktop();
    QRect screenRect = desktopWidget->screenGeometry();

    int showWidth = 1200;
    int showHeight = 900;
    int offsetX = (screenRect.width() - showWidth) / 2;
    int offsetY = (screenRect.height() - showHeight) / 2;

    MainWindow w;
    w.setGeometry(offsetX, offsetY, showWidth, showHeight);
    w.setMinimumSize(640, 480);
    w.show();
//  w.showMaximized();

    return a.exec();
}
