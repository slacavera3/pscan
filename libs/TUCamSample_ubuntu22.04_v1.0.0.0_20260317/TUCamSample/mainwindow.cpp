#include "mainwindow.h"
#include "ui_mainwindow.h"

#include <QHBoxLayout>
#include <QVBoxLayout>
#include <QScrollArea>

#include "camroi.h"
#include "camobject.h"
#include "camtrigger.h"
#include "camimagesave.h"
#include "caminformation.h"
#include "cammaincontrol.h"
#include "drawingglwidget.h"
#include "camoutputtrigger.h"
#include "camimageadjustment.h"

MainWindow::MainWindow(QWidget *parent) :
    QMainWindow(parent),
    ui(new Ui::MainWindow),
    m_ctrlPanel(NULL),
    m_leftWidget(NULL),
    m_camRoi(NULL),
    m_camTrigger(NULL),
    m_camOutputTrigger(NULL),
    m_camInformation(NULL),
    m_camMainControl(NULL),
    m_drawWidget(NULL)
{
    ui->setupUi(this);
    this->setWindowTitle("TUCamSample");

    initUI();
    bindConnections();
}

MainWindow::~MainWindow()
{
    delete ui;
}

void MainWindow::initUI()
{
    initLeftPanel();
    updatePanelSize();

    m_drawWidget = new DrawingGLWidget();

    QHBoxLayout *hBoxLayout = new QHBoxLayout();
    hBoxLayout->setContentsMargins(2, 2, 2, 2);
    hBoxLayout->addWidget(m_leftWidget);
    hBoxLayout->addWidget(m_drawWidget);

    centralWidget()->setLayout(hBoxLayout);
}

void MainWindow::initLeftPanel()
{
    m_camRoi = new CamRoi(this);
    m_camTrigger = new CamTrigger(this);
    m_camImageSave = new CamImageSave(this);
    m_camInformation = new CamInformation(this);
    m_camMainControl = new CamMainControl(this);
    m_camOutputTrigger = new CamOutputTrigger(this);
    m_camImageAdjustment = new CamImageAdjustment(this);

    m_widgetsList.clear();
    m_widgetsList.push_back(m_camInformation);
    m_widgetsList.push_back(m_camMainControl);
    m_widgetsList.push_back(m_camRoi);
    m_widgetsList.push_back(m_camImageSave);
    m_widgetsList.push_back(m_camImageAdjustment);
    m_widgetsList.push_back(m_camTrigger);
    m_widgetsList.push_back(m_camOutputTrigger);

    QVBoxLayout *vBoxLayout = new QVBoxLayout();
    vBoxLayout->setContentsMargins(2, 2, 2, 2);
    vBoxLayout->addWidget(m_camInformation);
    vBoxLayout->addWidget(m_camMainControl);
    vBoxLayout->addWidget(m_camRoi);
    vBoxLayout->addWidget(m_camImageSave);
    vBoxLayout->addWidget(m_camTrigger);
    vBoxLayout->addWidget(m_camOutputTrigger);
    vBoxLayout->addWidget(m_camImageAdjustment);

    m_ctrlPanel = new QWidget();
    m_ctrlPanel->setLayout(vBoxLayout);
    m_ctrlPanel->adjustSize();

    QScrollArea *scrollArea = new QScrollArea();
    scrollArea->setFrameShape(QFrame::NoFrame);
    scrollArea->setWidget(m_ctrlPanel);

    m_leftWidget = new QWidget();
    QVBoxLayout *leftWidgetBoxLayout = new QVBoxLayout(m_leftWidget);
    leftWidgetBoxLayout->setContentsMargins(0, 0, 0, 0);
    leftWidgetBoxLayout->addWidget(scrollArea);
    m_leftWidget->setFixedSize(360/*300*/, 800);
}

void MainWindow::updatePanelSize()
{
    int height = 0;
    int count = m_widgetsList.size();

    for (int i = 0; i < count; ++i)
    {
        height += m_widgetsList[i]->geometry().height();
    }

    m_ctrlPanel->setFixedSize(340/*280*/, height);
}

void MainWindow::resizeEvent(QResizeEvent *event)
{
    if(NULL == m_leftWidget)
        return;

    m_leftWidget->setFixedSize(360/*300*/, this->geometry().height());
}

void MainWindow::bindConnections()
{
    connect(CamObject::getInstance()->getWaittingThread(), SIGNAL(signalUpdateFrameRate(double)), m_widgetsList[1], SLOT(slotUpdateFrameRate(double)));
    connect(CamObject::getInstance()->getWaittingThread(), SIGNAL(signalUpdateImage(QImage)), m_drawWidget, SLOT(slotUpdateImage(QImage)));
    connect(m_camMainControl, SIGNAL(signalUpdateLevelRange()), m_camImageAdjustment, SLOT(slotUpdateLevelRange()));
}
