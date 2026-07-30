#include "camroi.h"
#include "ui_camroi.h"

#include <QPainter>

#include "camobject.h"

CamRoi::CamRoi(QWidget *parent) :
    QWidget(parent),
    ui(new Ui::CamRoi),
    m_isRoi(false),
    m_maxWidth(0),
    m_maxHeight(0)
{
    ui->setupUi(this);
    this->setLayout(ui->gridLayout);
    this->setMaximumHeight(130);

    initRoiParameter();
}

CamRoi::~CamRoi()
{
    delete ui;
}

void CamRoi::initRoiParameter()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_VALUE_INFO valueInfo;
    valueInfo.nValue = 1;
    valueInfo.nTextSize = 0;
    valueInfo.pText = NULL;

    valueInfo.nID = TUIDI_CURRENT_WIDTH;
    TUCAM_Dev_GetInfo(hIdxTUCam, &valueInfo);
    m_maxWidth = valueInfo.nValue;

    valueInfo.nID = TUIDI_CURRENT_HEIGHT;
    TUCAM_Dev_GetInfo(hIdxTUCam, &valueInfo);
    m_maxHeight = valueInfo.nValue;

    ui->leHOffset->setText("0");
    ui->leVOffset->setText("0");
    ui->leWidth->setText(QString::number(100));
    ui->leHeight->setText(QString::number(100));

    updateRoiState(false);
}

void CamRoi::updateRoiState(bool isRoi)
{
    ui->labnWidth->setEnabled(!isRoi);
    ui->labnHeight->setEnabled(!isRoi);
    ui->labnHOffset->setEnabled(!isRoi);
    ui->labnVOffset->setEnabled(!isRoi);

    ui->leWidth->setEnabled(!isRoi);
    ui->leHeight->setEnabled(!isRoi);
    ui->leHOffset->setEnabled(!isRoi);
    ui->leVOffset->setEnabled(!isRoi);

    ui->pbSet->setText(isRoi ? tr("Set Full") : tr("Set ROI"));
}

void CamRoi::paintEvent(QPaintEvent *event)
{
    Q_UNUSED(event);

    QPainter painter(this);
    QRect drawRect(0, 0, this->geometry().width(), this->geometry().height());
    drawRect.setTop(drawRect.top() + 24);
    drawRect.setRight(drawRect.right() - 1);
    drawRect.setBottom(drawRect.bottom() - 2);
    painter.drawRect(drawRect);
}


void CamRoi::on_pbSet_clicked()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    bool isWaitting = CamObject::getInstance()->isWaitting();
    CamObject::getInstance()->stopWaittingForFrames();

    TUCAM_ROI_ATTR roiAttr;

    if (m_isRoi)
    {
        m_isRoi = false;

        roiAttr.bEnable = m_isRoi;
        roiAttr.nVOffset = 0;
        roiAttr.nHOffset = 0;
        roiAttr.nWidth = m_maxWidth;
        roiAttr.nHeight = m_maxHeight;

        TUCAM_Cap_SetROI(hIdxTUCam, roiAttr);
    }
    else
    {
        m_isRoi = true;

        roiAttr.bEnable = m_isRoi;
        roiAttr.nWidth = ((ui->leWidth->text().toInt() >> 2) << 2);
        roiAttr.nHeight = ((ui->leHeight->text().toInt() >> 2) << 2);
        roiAttr.nVOffset = ((ui->leVOffset->text().toInt() >> 2) << 2);
        roiAttr.nHOffset = ((ui->leHOffset->text().toInt() >> 2) << 2);

        TUCAM_Cap_SetROI(hIdxTUCam, roiAttr);
        TUCAM_Cap_GetROI(hIdxTUCam, &roiAttr);

        ui->leWidth->setText(QString::number(roiAttr.nWidth));
        ui->leHeight->setText(QString::number(roiAttr.nHeight));
        ui->leHOffset->setText(QString::number(roiAttr.nHOffset));
        ui->leVOffset->setText(QString::number(roiAttr.nVOffset));
    }

    updateRoiState(m_isRoi);

    if (isWaitting)
    {
        CamObject::getInstance()->startWaittingForFrames();
    }
}
