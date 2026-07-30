#include "cammaincontrol.h"
#include "ui_cammaincontrol.h"

#include <QPainter>

CamMainControl::CamMainControl(QWidget *parent) :
    QWidget(parent),
    ui(new Ui::CamMainControl),
    m_isSupportMs(false),
    m_isSupportSec(false),
    m_isSupportAeTarget(false),
    m_aeTimerId(0),
    m_tempTimerId(0)
{
    ui->setupUi(this);
    this->setLayout(ui->gridLayout);
    this->setMaximumHeight(480);

    initRange();
    updateValue();
}

CamMainControl::~CamMainControl()
{
    if (0 != m_tempTimerId)
    {
        killTimer(m_tempTimerId);
    }
    delete ui;
}

void CamMainControl::initRange()
{
    int pid = CamObject::getInstance()->getCamPID();
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    char text[64] = {0};
    TUCAM_VALUE_TEXT valueText;
    valueText.nTextSize = 64;
    valueText.pText = text;

    // Get resolution range
    m_resolutionCapa.nValDft = 0;
    m_resolutionCapa.nValMin = 0;
    m_resolutionCapa.nValMax = 0;
    m_resolutionCapa.nValStep = 0;
    m_resolutionCapa.idCapa = TUIDC_RESOLUTION;
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetAttr(hIdxTUCam, &m_resolutionCapa))
    {
        ui->cbbResolution->blockSignals(true);
        ui->cbbResolution->clear();

        int nCnt = m_resolutionCapa.nValMax - m_resolutionCapa.nValMin + 1;

        valueText.nID = TUIDC_RESOLUTION;
        for (int i = 0; i < nCnt; ++i)
        {
            valueText.dbValue = i;
            TUCAM_Capa_GetValueText(hIdxTUCam, &valueText);
            ui->cbbResolution->addItem(valueText.pText);
        }
        ui->cbbResolution->blockSignals(false);
    }

    // Get bin range
    m_binCapa.nValDft = 0;
    m_binCapa.nValMin = 0;
    m_binCapa.nValMax = 0;
    m_binCapa.nValStep = 0;
    m_binCapa.idCapa = TUIDC_BINNING_SUM;
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetAttr(hIdxTUCam, &m_binCapa))
    {
        ui->cbbBin->blockSignals(true);
        ui->cbbBin->clear();

        int nCnt = m_binCapa.nValMax - m_binCapa.nValMin + 1;

        valueText.nID = TUIDC_BINNING_SUM;
        for (int i = 0; i < nCnt; ++i)
        {
            valueText.dbValue = i;
            TUCAM_Capa_GetValueText(hIdxTUCam, &valueText);
            ui->cbbBin->addItem(valueText.pText);
        }
        ui->cbbBin->blockSignals(false);
    }
    else
    {
        ui->labnBin->hide();
        ui->cbbBin->hide();
    }

    // mode
    m_modeCapa.idCapa = TUIDC_IMGMODESELECT;
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetAttr(hIdxTUCam, &m_modeCapa))
    {
        ui->cmbMode->blockSignals(true);
        ui->cmbMode->clear();

        int nCnt = m_modeCapa.nValMax - m_modeCapa.nValMin + 1;

        valueText.nID = TUIDC_IMGMODESELECT;
        for (int i = 0; i < nCnt; ++i)
        {
            valueText.dbValue = i;
            TUCAM_Capa_GetValueText(hIdxTUCam, &valueText);
            ui->cmbMode->addItem(valueText.pText);
        }
        ui->cmbMode->blockSignals(false);
    }
    else
    {
        ui->labMode->hide();
        ui->cmbMode->hide();
        ui->widget->hide();
    }

    // shutter
    m_shutterCapa.idCapa = TUIDC_SHUTTER;
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetAttr(hIdxTUCam, &m_shutterCapa))
    {
        ui->cmbShutter->blockSignals(true);
        ui->cmbShutter->clear();

        int nCnt = m_shutterCapa.nValMax - m_shutterCapa.nValMin + 1;

        valueText.nID = TUIDC_SHUTTER;
        for (int i = 0; i < nCnt; ++i)
        {
            valueText.dbValue = i;
            TUCAM_Capa_GetValueText(hIdxTUCam, &valueText);
            ui->cmbShutter->addItem(valueText.pText);
        }
        ui->cmbShutter->blockSignals(false);
    }
    else
    {
        ui->labShutter->hide();
        ui->cmbShutter->hide();
        ui->widget_2->hide();
    }

    // Get gain range
    m_gainProp.dbValDft = 0;
    m_gainProp.dbValMin = 0;
    m_gainProp.dbValMax = 0;
    m_gainProp.dbValStep = 0;
    m_gainProp.nIdxChn = 0;
    m_gainProp.idProp = TUIDP_GLOBALGAIN;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_gainProp))
    {
        ui->hsldGrian->setRange((int)m_gainProp.dbValMin, (int)m_gainProp.dbValMax);
    }

    // Get auto exposure
    m_aeCapa.nValDft = 0;
    m_aeCapa.nValMin = 0;
    m_aeCapa.nValMax = 0;
    m_aeCapa.nValStep = 0;
    m_aeCapa.idCapa = TUIDC_ATEXPOSURE;
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetAttr(hIdxTUCam, &m_aeCapa))
    {
        ui->cbAE->setEnabled(true);
    }
    else
    {
        ui->cbAE->setEnabled(false);
    }

    // Get auto exposure time target range
    m_aeTargetProp.dbValDft = 0;
    m_aeTargetProp.dbValMin = 0;
    m_aeTargetProp.dbValMax = 0;
    m_aeTargetProp.dbValStep = 0;
    m_aeTargetProp.idProp = TUIDP_BRIGHTNESS;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_aeTargetProp))
    {
        ui->hsldBrightness->setEnabled(true);
        ui->labvBrightness->setEnabled(true);

        m_isSupportAeTarget = true;
        ui->hsldBrightness->setRange((int)m_aeTargetProp.dbValMin, (int)m_aeTargetProp.dbValMax);

        // MIchrome camera series must set TUIDC_ATEXPOSURE_MODE to use auto exposure time target
        TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_ATEXPOSURE_MODE, 3);
    }
    else
    {
        ui->hsldBrightness->setEnabled(false);
        ui->labvBrightness->setEnabled(false);

        m_isSupportAeTarget = false;
    }

    // Get exposure time range
    m_isSupportMs = true;
    m_isSupportSec = true;

    m_exposureProp.dbValDft = 0;
    m_exposureProp.dbValMin = 0;
    m_exposureProp.dbValMax = 0;
    m_exposureProp.dbValStep = 0;
    m_exposureProp.idProp = TUIDP_EXPOSURETM;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_exposureProp))
    {
        ui->labnUs->setEnabled(true);
        ui->labnMs->setEnabled(true);
        ui->labnSec->setEnabled(true);

        ui->spbUs->setEnabled(true);
        ui->spbMs->setEnabled(true);
        ui->spbSec->setEnabled(true);

        if (PID_MICHROME5PRO == pid || PID_MICHROME6 == pid)
        {
            m_exposureProp.dbValMin = 0.13;
            m_exposureProp.dbValMax = 15000000;
        }

//      uint expStep = (uint)(m_exposureProp.dbValStep * 1000 + 0.5f);

        if ((uint)(m_exposureProp.dbValMax * 1000) < 1000000)
        {
            m_isSupportSec = false;

            ui->spbSec->setEnabled(false);
            ui->labnSec->setEnabled(false);
        }

        if ((uint)(m_exposureProp.dbValMax * 1000) < 1000)
        {
            m_isSupportMs = false;
            m_isSupportSec = false;

            ui->spbMs->setEnabled(false);
            ui->spbSec->setEnabled(false);

            ui->spbMs->setEnabled(false);
            ui->labnSec->setEnabled(false);
        }

        ui->spbUs->setRange(0, 999);
        ui->spbMs->setRange(0, 999);
        ui->spbSec->setRange(0, (uint)(m_exposureProp.dbValMax / 1000));
    }

    m_temperatureProp.nIdxChn= 0;
    m_temperatureProp.idProp = TUIDP_TEMPERATURE;
    if (TUCAMRET_SUCCESS != TUCAM_Prop_GetAttr(hIdxTUCam, &m_temperatureProp))
    {
        ui->labTemp->hide();
        ui->labTempValue->hide();
        ui->labUnit->hide();
        ui->widget_3->hide();

        ui->labelCoolingTxt->hide();
        ui->horizontalSliderCooling->hide();
        ui->labelCoolingValue->hide();
    }
    else
    {
        if (0 == m_tempTimerId)
        {
            m_tempTimerId = this->startTimer(1000);
        }
        ui->horizontalSliderCooling->blockSignals(true);
        ui->horizontalSliderCooling->setRange(m_temperatureProp.dbValMin, m_temperatureProp.dbValMax);
        ui->horizontalSliderCooling->blockSignals(false);
        m_midTemp = 0;
        TUCAM_VALUE_INFO valueInfo;
        valueInfo.nID = TUIDI_ZEROTEMPERATURE_VALUE;
        if (TUCAMRET_SUCCESS == TUCAM_Dev_GetInfo(hIdxTUCam, &valueInfo))
        {
            m_midTemp = valueInfo.nValue;
        }
    }

    m_frameRateProp.nIdxChn= 0;
    m_frameRateProp.idProp = TUIDP_FRAME_RATE;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_frameRateProp))
    {
        ui->doubleSpinBoxFrameRate->setRange(m_frameRateProp.dbValMin, m_frameRateProp.dbValMax);
    }
    else
    {
        ui->labelFrameRate->hide();
        ui->doubleSpinBoxFrameRate->hide();
        ui->pushButtonFrameRate->hide();
    }

    m_maxAEProp.nIdxChn= 0;
    m_maxAEProp.idProp = TUIDP_EXPOSUREMAX;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_maxAEProp))
    {
        ui->spinBoxMaxAutoExp->setRange((int)m_maxAEProp.dbValMin, (int)m_maxAEProp.dbValMax);
    }
    else
    {
        ui->labelMaxAutoExp->hide();
        ui->spinBoxMaxAutoExp->hide();
        ui->pushButtonMaxAutoExp->hide();
    }
}

void CamMainControl::updateValue()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    int paramValue = 0;
    double dbValue = 0;

    // Get the current resolution value
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetValue(hIdxTUCam, TUIDC_ENABLELED, &paramValue))
    {
        ui->checkBoxLed->setChecked(paramValue);
    }
    else
    {
        ui->checkBoxLed->hide();
    }

    // Get the current resolution value
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetValue(hIdxTUCam, TUIDC_RESOLUTION, &paramValue))
    {
        ui->cbbResolution->blockSignals(true);
        ui->cbbResolution->setCurrentIndex(paramValue);
        ui->cbbResolution->blockSignals(false);
    }

    // Get the current bin value
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetValue(hIdxTUCam, TUIDC_BINNING_SUM, &paramValue))
    {
        ui->cbbBin->blockSignals(true);
        ui->cbbBin->setCurrentIndex(paramValue);
        ui->cbbBin->blockSignals(false);
    }

    // Get the current mode value
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetValue(hIdxTUCam, TUIDC_IMGMODESELECT, &paramValue))
    {
        ui->cmbMode->blockSignals(true);
        ui->cmbMode->setCurrentIndex(paramValue);
        ui->cmbMode->blockSignals(false);
    }

    // Get the current shutter value
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetValue(hIdxTUCam, TUIDC_SHUTTER, &paramValue))
    {
        ui->cmbShutter->blockSignals(true);
        ui->cmbShutter->setCurrentIndex(paramValue);
        ui->cmbShutter->blockSignals(false);
    }

    // Get the current cooling value
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_TEMPERATURE_TARGET, &dbValue, 0))
    {
        ui->horizontalSliderCooling->blockSignals(true);
        ui->horizontalSliderCooling->setValue((int)dbValue);
        ui->horizontalSliderCooling->blockSignals(false);
        ui->labelCoolingValue->setText(QString::number(m_midTemp > 100 ? ((dbValue - m_midTemp) / 10.0) : (dbValue - m_midTemp)));
    }

    // Get the current framerate value
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_FRAME_RATE, &dbValue, 0))
    {
        ui->doubleSpinBoxFrameRate->setValue(dbValue);
    }


    // Get the current aemax value
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_EXPOSUREMAX, &dbValue, 0))
    {
        ui->spinBoxMaxAutoExp->setValue((int)dbValue);
    }

    updateAutoExposureState();
}

void CamMainControl::updateExposureTime(uint exposureTime)
{
    int pid = CamObject::getInstance()->getCamPID();

    if (PID_MICHROME5PRO == pid || PID_MICHROME6 == pid)
    {
        if (exposureTime < 137)
            exposureTime = 130;

        if (exposureTime > 15000000)
            exposureTime = 15000000;
    }

    uint secValue = (uint)(exposureTime / (1000 * 1000));
    uint msValue = (uint)(exposureTime / 1000) % 1000;
    uint usValue = (uint)exposureTime % 1000;

    ui->spbSec->setValue(secValue);
    ui->spbMs->setValue(msValue);
    ui->spbUs->setValue(usValue);
}

void CamMainControl::updateAutoExposureState()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    int aeState = 0;
    double gainValue = 0;
    double aeTargetValue = 0;
    double exposureValue = 0;

    bool isAeState = false;

    // Get the current auto exposure time state
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetValue(hIdxTUCam, TUIDC_ATEXPOSURE, &aeState))
    {
        isAeState = (0 != aeState) ? true : false;

        if (isAeState)
        {
            ui->cbAE->setChecked(true);
            ui->pbOk->setEnabled(false);
            ui->labnExposure->setEnabled(false);

            ui->labnUs->setEnabled(false);
            ui->labnMs->setEnabled(false);
            ui->labnSec->setEnabled(false);

            ui->spbUs->setEnabled(false);
            ui->spbMs->setEnabled(false);
            ui->spbSec->setEnabled(false);

            if (0 == m_aeTimerId)
            {
                m_aeTimerId = this->startTimer(500);
            }
        }
        else
        {
            ui->cbAE->setChecked(false);
            ui->pbOk->setEnabled(true);
            ui->labnExposure->setEnabled(true);

            ui->labnUs->setEnabled(true);
            ui->labnMs->setEnabled(m_isSupportMs);
            ui->labnSec->setEnabled(m_isSupportSec);

            ui->spbUs->setEnabled(true);
            ui->spbMs->setEnabled(m_isSupportMs);
            ui->spbSec->setEnabled(m_isSupportSec);

            if (0 != m_aeTimerId)
            {
                killTimer(m_aeTimerId);
                m_aeTimerId = 0;
            }
        }
    }

    // Get the current gain value
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_GLOBALGAIN, &gainValue, 0))
    {
        ui->hsldGrian->setValue((int)gainValue);
        ui->labvGrain->setText(QString::number(gainValue));

        ///ui->hsldGrian->setEnabled(!isAeState);
        ///ui->labnGrain->setEnabled(!isAeState);
        ///ui->labvGrain->setEnabled(!isAeState);

        ui->hsldGrian->setEnabled(true);
        ui->labnGrain->setEnabled(true);
        ui->labvGrain->setEnabled(true);
    }
    else
    {
        ui->hsldGrian->setEnabled(false);
        ui->labnGrain->setEnabled(false);
        ui->labvGrain->setEnabled(false);
    }

    // Get the current target value
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_BRIGHTNESS, &aeTargetValue, 0))
    {
        ui->hsldBrightness->setValue((int)aeTargetValue);
        ui->labvBrightness->setText(QString::number(aeTargetValue));

        ui->hsldBrightness->setEnabled(m_isSupportAeTarget && isAeState);
        ui->labvBrightness->setEnabled(m_isSupportAeTarget && isAeState);
    }
    else
    {
        ui->hsldBrightness->setEnabled(false);
        ui->labvBrightness->setEnabled(false);
    }

    // Get the current exposure time value
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_EXPOSURETM, &exposureValue, 0))
    {
        updateExposureTime((int)(exposureValue * 1000));
    }
}

void CamMainControl::paintEvent(QPaintEvent *event)
{
    Q_UNUSED(event);

    QPainter painter(this);
    QRect drawRect(0, 0, this->geometry().width(), this->geometry().height());
    drawRect.setTop(drawRect.top() + 24);
    drawRect.setRight(drawRect.right() - 1);
    drawRect.setBottom(drawRect.bottom() - 2);
    painter.drawRect(drawRect);
}

void CamMainControl::timerEvent(QTimerEvent *event)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    double paramValue = 0;
    if (event->timerId() == m_aeTimerId)
    {
        if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_GLOBALGAIN, &paramValue, 0))
        {
            if (m_gainProp.dbValMax > 6)
            {
                ui->hsldGrian->setValue((int)paramValue);
                ui->labvGrain->setText(QString::number(ui->hsldGrian->value()));
            }
        }

        if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_EXPOSURETM, &paramValue, 0))
        {
            updateExposureTime((int)(paramValue * 1000));
        }
    }

    if (event->timerId() == m_tempTimerId)
    {
        if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_TEMPERATURE, &paramValue, 0))
        {
            ui->labTempValue->setText(QString::number(paramValue, 'f', 2));
        }
    }
}

void CamMainControl::slotUpdateFrameRate(double frameRate)
{
    ui->labvFps->setText(QString::number(frameRate, 'f', 2) + " fps");
}

void CamMainControl::on_pbLive_clicked()
{
    CamObject *camObject = CamObject::getInstance();

    if (camObject->isWaitting())
    {
        camObject->stopWaittingForFrames();
        ui->pbLive->setText(tr("Live"));
    }
    else
    {
        camObject->startWaittingForFrames();
        ui->pbLive->setText(tr("Stop"));
    }
}

void CamMainControl::on_cbbResolution_currentIndexChanged(int index)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    bool isWaitting = CamObject::getInstance()->isWaitting();

    CamObject::getInstance()->stopWaittingForFrames();
    TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_RESOLUTION, index);

    emit signalUpdateLevelRange();

    initRange();
    updateValue();

    if (isWaitting)
    {
        CamObject::getInstance()->startWaittingForFrames();
    }
}

void CamMainControl::on_cbbBin_currentIndexChanged(int index)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    bool isWaitting = CamObject::getInstance()->isWaitting();

    CamObject::getInstance()->stopWaittingForFrames();
    TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_BINNING_SUM, index);

    emit signalUpdateLevelRange();

    if (isWaitting)
    {
        CamObject::getInstance()->startWaittingForFrames();
    }
}

void CamMainControl::on_hsldGrian_valueChanged(int value)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_GLOBALGAIN, value, 0);
    ui->labvGrain->setText(QString::number(value));
}

void CamMainControl::on_hsldBrightness_valueChanged(int value)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_BRIGHTNESS, value, 0);
    ui->labvBrightness->setText(QString::number(value));
}

void CamMainControl::on_cbAE_clicked(bool checked)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_ATEXPOSURE, (checked ? 1 : 0));

    updateAutoExposureState();

    if (checked)
    {
        if (0 == m_aeTimerId)
        {
            m_aeTimerId = this->startTimer(500);
        }
    }
    else
    {
        if (0 != m_aeTimerId)
        {
            killTimer(m_aeTimerId);
            m_aeTimerId = 0;
        }
    }
}

void CamMainControl::on_pbOk_clicked()
{
    int pid = CamObject::getInstance()->getCamPID();
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    int exposure = ui->spbSec->value() * 1000 * 1000 + ui->spbMs->value() * 1000 + ui->spbUs->value();

    if (PID_MICHROME5PRO == pid || PID_MICHROME6 == pid)
    {
        if (exposure < 137)
            exposure = 130;

        if (exposure > 15000000)
            exposure = 15000000;
    }
    else
    {
        if (exposure > 60000000)
            exposure = 60000000;
    }


    double getExposure = 0;
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_EXPOSURETM, (double)(exposure / 1000.0f), 0);
    TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_EXPOSURETM, &getExposure, 0);

    updateExposureTime((int)(getExposure * 1000));

}

void CamMainControl::on_cmbShutter_currentIndexChanged(int index)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_SHUTTER, index);
}

void CamMainControl::on_cmbMode_currentIndexChanged(int index)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    bool isWaitting = CamObject::getInstance()->isWaitting();

    CamObject::getInstance()->stopWaittingForFrames();
    TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_IMGMODESELECT, index);

    emit signalUpdateLevelRange();

    if (isWaitting)
    {
        CamObject::getInstance()->startWaittingForFrames();
    }
}

void CamMainControl::on_horizontalSliderCooling_valueChanged(int value)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_TEMPERATURE_TARGET, (double)value, 0);
    ui->labelCoolingValue->setText(QString::number(m_midTemp > 100 ? ((value - m_midTemp) / 10.0) : (value - m_midTemp)));
}

void CamMainControl::on_pushButtonFrameRate_clicked()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    double dbRate = ui->doubleSpinBoxFrameRate->value();
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_FRAME_RATE, (double)dbRate, 0);
    TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_FRAME_RATE, &dbRate, 0);
    ui->doubleSpinBoxFrameRate->setValue(dbRate);
}

void CamMainControl::on_pushButtonMaxAutoExp_clicked()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    double dbAutoMax = ui->spinBoxMaxAutoExp->value();
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_EXPOSUREMAX, (double)dbAutoMax, 0);
    TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_EXPOSUREMAX, &dbAutoMax, 0);
    ui->spinBoxMaxAutoExp->setValue((int)dbAutoMax);
}

void CamMainControl::on_checkBoxLed_clicked(bool checked)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

   TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_ENABLELED, checked);
}
