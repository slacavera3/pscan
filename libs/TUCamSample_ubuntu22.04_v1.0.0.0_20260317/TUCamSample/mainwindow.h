#ifndef MAINWINDOW_H
#define MAINWINDOW_H

#include <QMainWindow>

namespace Ui {
class MainWindow;
}

class CamRoi;
class CamTrigger;
class CamImageSave;
class CamInformation;
class CamMainControl;
class DrawingGLWidget;
class CamOutputTrigger;
class CamImageAdjustment;

class MainWindow : public QMainWindow
{
    Q_OBJECT

public:
    explicit MainWindow(QWidget *parent = 0);
    ~MainWindow();

    void initUI();
    void initLeftPanel();
    void updatePanelSize();
    void bindConnections();

protected:
    void resizeEvent(QResizeEvent *event);

private:
    Ui::MainWindow *ui;

    QVector<QWidget *> m_widgetsList;

    QWidget *m_ctrlPanel;
    QWidget *m_leftWidget;

    CamRoi *m_camRoi;
        CamTrigger *m_camTrigger;
    CamImageSave *m_camImageSave;
    CamInformation *m_camInformation;
    CamMainControl *m_camMainControl;
    CamOutputTrigger *m_camOutputTrigger;
    CamImageAdjustment *m_camImageAdjustment;

    DrawingGLWidget *m_drawWidget;
};

#endif // MAINWINDOW_H
