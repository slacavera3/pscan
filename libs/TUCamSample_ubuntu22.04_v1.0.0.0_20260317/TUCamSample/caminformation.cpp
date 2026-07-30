#include "caminformation.h"
#include "ui_caminformation.h"

#include <QPainter>

#include "camobject.h"

CamInformation::CamInformation(QWidget *parent) :
    QWidget(parent),
    ui(new Ui::CamInformation)
{
    ui->setupUi(this);
    this->setLayout(ui->gridLayout);
    this->setMaximumHeight(140);

    updateInformation();
}

CamInformation::~CamInformation()
{
    delete ui;
}

void CamInformation::paintEvent(QPaintEvent *event)
{
    Q_UNUSED(event);

    QPainter painter(this);
    QRect drawRect(0, 0, this->geometry().width(), this->geometry().height());
    drawRect.setTop(drawRect.top() + 24);
    drawRect.setRight(drawRect.right() - 1);
    drawRect.setBottom(drawRect.bottom() - 2);
    painter.drawRect(drawRect);
}

void CamInformation::updateInformation()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    // Get camera sn
    char snVal[64] = {0};
    TUCAM_REG_RW regRW;
    regRW.nBufSize = 64;
    regRW.pBuf = snVal;
    regRW.nRegType = TUREG_SN;
    if (TUCAMRET_SUCCESS == TUCAM_Reg_Read(hIdxTUCam, regRW))
    {
        ui->labvSn->setText(regRW.pBuf);
    }
    else
    {
        ui->labvSn->setText("");
    }

    // Get camera name
    TUCAM_VALUE_INFO valueInfo;
    valueInfo.nID = TUIDI_CAMERA_MODEL;
    valueInfo.nTextSize = 64;
    valueInfo.nValue = 0;
    valueInfo.pText = NULL;
    if (TUCAMRET_SUCCESS == TUCAM_Dev_GetInfo(hIdxTUCam, &valueInfo))
    {
        ui->labvName->setText(valueInfo.pText);
    }

    // Get firmware version
    valueInfo.nID = TUIDI_VERSION_FRMW;
    if (TUCAMRET_SUCCESS == TUCAM_Dev_GetInfo(hIdxTUCam, &valueInfo))
    {
        if (valueInfo.nValue == 0)
        {
            ui->labvFirmware->setText(valueInfo.pText);
        }
        else
        {
            ui->labvFirmware->setText(QString::number(valueInfo.nValue, 16));
        }

    }

    // Get usb type
    valueInfo.nID = TUIDI_BUS;
    if (TUCAMRET_SUCCESS == TUCAM_Dev_GetInfo(hIdxTUCam, &valueInfo))
    {
        if (0x200 == valueInfo.nValue || 0x210 == valueInfo.nValue)
        {
            ui->labvUsb->setText("2.0");
        }
        else
        {
            ui->labvUsb->setText("3.0");
        }
    }

    // Get sdk version
    valueInfo.nID = TUIDI_VERSION_API;
    if (TUCAMRET_SUCCESS == TUCAM_Dev_GetInfo(hIdxTUCam, &valueInfo))
    {
        ui->labvSdk->setText(valueInfo.pText);
    }
}
