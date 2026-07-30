#include "camtrigger.h"
#include "ui_camtrigger.h"

#include <QPainter>

CamTrigger::CamTrigger(QWidget *parent) :
    QWidget(parent),
    ui(new Ui::CamTrigger)
{   
    ui->setupUi(this);
    this->setLayout(ui->gridLayout_2);
    this->setMaximumHeight(360);

    initRange();
    updateValue();
}

CamTrigger::~CamTrigger()
{
    delete ui;
}

void CamTrigger::initRange()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    m_triggerAttr.nTgrMode = TUCCM_SEQUENCE;
    m_triggerAttr.nFrames = 1;
    m_triggerAttr.nDelayTm = 0;
    m_triggerAttr.nExpMode = TUCTE_EXPTM;
    m_triggerAttr.nEdgeMode = TUCTD_RISING;

    if (TUCAMRET_SUCCESS == TUCAM_Cap_GetTrigger(hIdxTUCam, &m_triggerAttr))
    {
        enableTriggerControl(true);
        if(CamObject::getInstance()->isSupportFL9BW() || CamObject::getInstance()->isSupportFL9BWLT() || CamObject::getInstance()->isSupportFL26BW()
                ||CamObject::getInstance()->isSupportLibra16() ||CamObject::getInstance()->isSupportLibra22() ||CamObject::getInstance()->isSupportLibra25())
        {
            ui->rbSynchronization->hide();
            ui->rbGlobal->hide();
        }
    }
    else
    {
        enableTriggerControl();
    }
    totalFrameVisiable();
}

void CamTrigger::updateValue()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    if (TUCAMRET_SUCCESS == TUCAM_Cap_GetTrigger(hIdxTUCam, &m_triggerAttr))
    {
        updatedelayTime(m_triggerAttr.nDelayTm);
        CamObject::getInstance()->setTriggerMode(m_triggerAttr.nTgrMode);
        CamObject::getInstance()->setTriggerExp(m_triggerAttr.nExpMode);
        CamObject::getInstance()->setTriggerEdge(m_triggerAttr.nEdgeMode);
        CamObject::getInstance()->setDelayTime(m_triggerAttr.nDelayTm);

        ui->rbTimed->setChecked(TUCTE_EXPTM == m_triggerAttr.nExpMode);
        ui->rbWidth->setChecked(TUCTE_WIDTH == m_triggerAttr.nExpMode);

        ui->rbRising->setChecked(TUCTD_RISING == m_triggerAttr.nEdgeMode);
        ui->rbFalling->setChecked(TUCTD_FAILING == m_triggerAttr.nEdgeMode);
    }
}

void CamTrigger::updatedelayTime(uint exposureTime)
{
    uint secValue = (uint)(exposureTime / (1000 * 1000)) % 60;
    uint msValue = (uint)(exposureTime / 1000) % 1000;
    uint usValue = (uint)exposureTime % 1000;

    ui->spbSec->setValue(secValue);
    ui->spbMs->setValue(msValue);
    ui->spbUs->setValue(usValue);
}

void CamTrigger::enableTriggerControl(bool enabled /*= false*/)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    bool isSfwTrigger = false;

    TUCAM_TRIGGER_ATTR triggerAttr;

    triggerAttr.nTgrMode = TUCCM_SEQUENCE;
    triggerAttr.nFrames = 1;
    triggerAttr.nDelayTm = 0;
    triggerAttr.nExpMode = TUCTE_EXPTM;
    triggerAttr.nEdgeMode = TUCTD_RISING;

    if (enabled)
    {
        if (TUCAMRET_SUCCESS == TUCAM_Cap_GetTrigger(hIdxTUCam, &triggerAttr))
        {
            switch (triggerAttr.nTgrMode)
            {
            case TUCCM_TRIGGER_STANDARD:
                {
                    ui->rbOff->setChecked(false);
                    ui->rbStandard->setChecked(true);
                    ui->rbSynchronization->setChecked(false);
                    ui->rbGlobal->setChecked(false);
                    ui->cbSoftwareTrigger->setChecked(false);
                }
                break;
            case TUCCM_TRIGGER_SYNCHRONOUS:
                {
                    ui->rbOff->setChecked(false);
                    ui->rbStandard->setChecked(false);
                    ui->rbSynchronization->setChecked(true);
                    ui->rbGlobal->setChecked(false);
                    ui->cbSoftwareTrigger->setChecked(false);
                }
                break;
            case TUCCM_TRIGGER_GLOBAL:
                {
                    ui->rbOff->setChecked(false);
                    ui->rbStandard->setChecked(false);
                    ui->rbSynchronization->setChecked(false);
                    ui->rbGlobal->setChecked(true);
                    ui->cbSoftwareTrigger->setChecked(false);
                }
                break;
            case TUCCM_TRIGGER_SOFTWARE:
                {
                    ui->rbOff->setChecked(false);
                    ui->rbStandard->setChecked(false);
                    ui->rbSynchronization->setChecked(false);
                    ui->rbGlobal->setChecked(false);
                    ui->cbSoftwareTrigger->setChecked(true);
                    isSfwTrigger = true;
                }
                break;
            case TUCCM_SEQUENCE:
            default:
                {
                    ui->rbOff->setChecked(true);
                    ui->rbStandard->setChecked(false);
                    ui->rbSynchronization->setChecked(false);
                    ui->rbGlobal->setChecked(false);
                    ui->cbSoftwareTrigger->setChecked(false);
                }
                break;
            }
        }

        ui->rbTimed->setChecked(TUCTE_EXPTM == triggerAttr.nExpMode);
        ui->rbWidth->setChecked(TUCTE_WIDTH == triggerAttr.nExpMode);

        bool isStandardTimed = TUCCM_TRIGGER_STANDARD == triggerAttr.nTgrMode && TUCTE_EXPTM == triggerAttr.nExpMode;
        ui->labnDelay->setEnabled(isStandardTimed);
        ui->labnSec->setEnabled(isStandardTimed);
        ui->labnMs->setEnabled(isStandardTimed);
        ui->labnUs->setEnabled(isStandardTimed);
        ui->spbSec->setEnabled(isStandardTimed);
        ui->spbMs->setEnabled(isStandardTimed);
        ui->spbUs->setEnabled(isStandardTimed);
        ui->pbOk->setEnabled(isStandardTimed);

        ui->rbRising->setChecked(TUCTD_RISING == triggerAttr.nEdgeMode);
        ui->rbFalling->setChecked(TUCTD_FAILING == triggerAttr.nEdgeMode);
    }

    ui->rbOff->setEnabled(enabled && !isSfwTrigger);
    ui->rbStandard->setEnabled(enabled && !isSfwTrigger);
    ui->rbSynchronization->setEnabled(enabled && !isSfwTrigger);
    ui->rbGlobal->setEnabled(enabled && !isSfwTrigger);
    ui->rbTimed->setEnabled(enabled && !isSfwTrigger && TUCCM_TRIGGER_SYNCHRONOUS != triggerAttr.nTgrMode && TUCCM_SEQUENCE != triggerAttr.nTgrMode);
    ui->rbWidth->setEnabled(enabled && !isSfwTrigger && TUCCM_SEQUENCE != triggerAttr.nTgrMode);
    ui->rbRising->setEnabled(enabled && !isSfwTrigger && TUCCM_SEQUENCE != triggerAttr.nTgrMode);
    ui->rbFalling->setEnabled(enabled && !isSfwTrigger && TUCCM_SEQUENCE != triggerAttr.nTgrMode);

    ui->cbSoftwareTrigger->setEnabled(enabled);
    ui->pbApply->setEnabled(enabled);
    ui->pbSnap->setEnabled(enabled);
}

void CamTrigger::paintEvent(QPaintEvent *event)
{
    Q_UNUSED(event);

    QPainter painter(this);
    QRect drawRect(0, 0, this->geometry().width(), this->geometry().height());
    drawRect.setTop(drawRect.top() + 24);
    drawRect.setRight(drawRect.right() - 1);
    drawRect.setBottom(drawRect.bottom() - 2);
    painter.drawRect(drawRect);
}

void CamTrigger::on_rbOff_clicked()
{
    ui->cbSoftwareTrigger->setChecked(false);
    m_triggerAttr.nTgrMode = TUCCM_SEQUENCE;
    CamObject::getInstance()->setTriggerMode(TUCCM_SEQUENCE);

    ui->rbRising->setEnabled(false);
    ui->rbFalling->setEnabled(false);
    ui->rbTimed->setEnabled(false);
    ui->rbWidth->setEnabled(false);

    ui->labnDelay->setEnabled(false);
    ui->labnSec->setEnabled(false);
    ui->labnMs->setEnabled(false);
    ui->labnUs->setEnabled(false);
    ui->spbSec->setEnabled(false);
    ui->spbMs->setEnabled(false);
    ui->spbUs->setEnabled(false);
    ui->pbOk->setEnabled(false);
    totalFrameVisiable();
}

void CamTrigger::on_rbStandard_clicked()
{
    m_triggerAttr.nTgrMode = TUCCM_TRIGGER_STANDARD;
    CamObject::getInstance()->setTriggerMode(TUCCM_TRIGGER_STANDARD);

    ui->rbRising->setEnabled(true);
    ui->rbFalling->setEnabled(true);
    ui->rbTimed->setEnabled(true);
    ui->rbWidth->setEnabled(true);

    ui->labnDelay->setEnabled(TUCTE_EXPTM == m_triggerAttr.nExpMode);
    ui->labnSec->setEnabled(TUCTE_EXPTM == m_triggerAttr.nExpMode);
    ui->labnMs->setEnabled(TUCTE_EXPTM == m_triggerAttr.nExpMode);
    ui->labnUs->setEnabled(TUCTE_EXPTM == m_triggerAttr.nExpMode);
    ui->spbSec->setEnabled(TUCTE_EXPTM == m_triggerAttr.nExpMode);
    ui->spbMs->setEnabled(TUCTE_EXPTM == m_triggerAttr.nExpMode);
    ui->spbUs->setEnabled(TUCTE_EXPTM == m_triggerAttr.nExpMode);
    ui->pbOk->setEnabled(TUCTE_EXPTM == m_triggerAttr.nExpMode);
    totalFrameVisiable();
}

void CamTrigger::on_rbSynchronization_clicked()
{
    m_triggerAttr.nTgrMode = TUCCM_TRIGGER_SYNCHRONOUS;
    CamObject::getInstance()->setTriggerMode(TUCCM_TRIGGER_SYNCHRONOUS);
    ui->rbWidth->setChecked(true);
    ui->rbTimed->setChecked(false);

    ui->rbRising->setEnabled(true);
    ui->rbFalling->setEnabled(true);
    ui->rbTimed->setEnabled(false);
    ui->rbWidth->setEnabled(true);

    ui->labnDelay->setEnabled(false);
    ui->labnSec->setEnabled(false);
    ui->labnMs->setEnabled(false);
    ui->labnUs->setEnabled(false);
    ui->spbSec->setEnabled(false);
    ui->spbMs->setEnabled(false);
    ui->spbUs->setEnabled(false);
    ui->pbOk->setEnabled(false);
    totalFrameVisiable();
}

void CamTrigger::on_rbGlobal_clicked()
{
    m_triggerAttr.nTgrMode = TUCCM_TRIGGER_GLOBAL;
    CamObject::getInstance()->setTriggerMode(TUCCM_TRIGGER_GLOBAL);

    ui->rbRising->setEnabled(true);
    ui->rbFalling->setEnabled(true);
    ui->rbTimed->setEnabled(true);
    ui->rbWidth->setEnabled(true);

    ui->labnDelay->setEnabled(false);
    ui->labnSec->setEnabled(false);
    ui->labnMs->setEnabled(false);
    ui->labnUs->setEnabled(false);
    ui->spbSec->setEnabled(false);
    ui->spbMs->setEnabled(false);
    ui->spbUs->setEnabled(false);
    ui->pbOk->setEnabled(false);
    totalFrameVisiable();
}

void CamTrigger::on_rbTimed_clicked()
{
    m_triggerAttr.nExpMode = TUCTE_EXPTM;
    CamObject::getInstance()->setTriggerExp(TUCTE_EXPTM);

    ui->labnDelay->setEnabled(TUCCM_TRIGGER_GLOBAL != m_triggerAttr.nTgrMode);
    ui->labnSec->setEnabled(TUCCM_TRIGGER_GLOBAL != m_triggerAttr.nTgrMode);
    ui->labnMs->setEnabled(TUCCM_TRIGGER_GLOBAL != m_triggerAttr.nTgrMode);
    ui->labnUs->setEnabled(TUCCM_TRIGGER_GLOBAL != m_triggerAttr.nTgrMode);
    ui->spbSec->setEnabled(TUCCM_TRIGGER_GLOBAL != m_triggerAttr.nTgrMode);
    ui->spbMs->setEnabled(TUCCM_TRIGGER_GLOBAL != m_triggerAttr.nTgrMode);
    ui->spbUs->setEnabled(TUCCM_TRIGGER_GLOBAL != m_triggerAttr.nTgrMode);
    ui->pbOk->setEnabled(TUCCM_TRIGGER_GLOBAL != m_triggerAttr.nTgrMode);
    totalFrameVisiable();
}



void CamTrigger::on_rbWidth_clicked()
{
    m_triggerAttr.nExpMode = TUCTE_WIDTH;
    CamObject::getInstance()->setTriggerExp(TUCTE_WIDTH);

    ui->labnDelay->setEnabled(false);
    ui->labnSec->setEnabled(false);
    ui->labnMs->setEnabled(false);
    ui->labnUs->setEnabled(false);
    ui->spbSec->setEnabled(false);
    ui->spbMs->setEnabled(false);
    ui->spbUs->setEnabled(false);
    ui->pbOk->setEnabled(false);
    totalFrameVisiable();
}

void CamTrigger::on_rbRising_clicked()
{
    m_triggerAttr.nEdgeMode = TUCTD_RISING;
    CamObject::getInstance()->setTriggerEdge(TUCTD_RISING);
}

void CamTrigger::on_rbFalling_clicked()
{
    m_triggerAttr.nEdgeMode = TUCTD_FAILING;
    CamObject::getInstance()->setTriggerEdge(TUCTD_FAILING);
}

void CamTrigger::on_cbSoftwareTrigger_clicked(bool checked)
{
    m_triggerAttr.nTgrMode = checked ? TUCCM_TRIGGER_SOFTWARE : TUCCM_SEQUENCE;
    CamObject::getInstance()->setTriggerMode(checked ? TUCCM_TRIGGER_SOFTWARE : TUCCM_SEQUENCE);

    if (!checked)
    {
        ui->rbOff->setChecked(true);
        ui->rbStandard->setChecked(false);
        ui->rbSynchronization->setChecked(false);
        ui->rbGlobal->setChecked(false);
    }

    ui->rbOff->setEnabled(!checked);
    ui->rbStandard->setEnabled(!checked);
    ui->rbSynchronization->setEnabled(!checked);
    ui->rbGlobal->setEnabled(!checked);

    ui->rbRising->setEnabled(!checked);
    ui->rbFalling->setEnabled(!checked);
    ui->rbTimed->setEnabled(!checked);
    ui->rbWidth->setEnabled(!checked);

    ui->labnDelay->setEnabled(!checked);
    ui->labnSec->setEnabled(!checked);
    ui->labnMs->setEnabled(!checked);
    ui->labnUs->setEnabled(!checked);
    ui->spbSec->setEnabled(!checked);
    ui->spbMs->setEnabled(!checked);
    ui->spbUs->setEnabled(!checked);
    ui->pbOk->setEnabled(!checked);
}

void CamTrigger::on_pbOk_clicked()
{
    long delayTime = (long)ui->spbSec->value() * 1000 * 1000 + (long)ui->spbMs->value() * 1000 + (long)ui->spbUs->value();

    if (delayTime > 10000000)
    {
        delayTime = 10000000;
        updatedelayTime(delayTime);
    }

    CamObject::getInstance()->setDelayTime(delayTime);
    setTrigger();
}

void CamTrigger::on_pbSnap_clicked()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Cap_DoSoftwareTrigger(hIdxTUCam);
}

void CamTrigger::on_pbApply_clicked()
{
    on_pbOk_clicked();

    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    ui->pbApply->setEnabled(false);
    enableTriggerControl(false);

    setTrigger();

    ui->pbApply->setEnabled(true);
    enableTriggerControl(true);
}

void CamTrigger::setTrigger()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    bool isWaitting = CamObject::getInstance()->isWaitting();

    if (isWaitting)
    {
        CamObject::getInstance()->stopWaittingForFrames();
    }
    else
    {
        sleep(1);
    }

//  m_triggerAttr.nTgrMode = CamObject::getInstance()->getTriggerMode();
//  m_triggerAttr.nExpMode = CamObject::getInstance()->getTriggerExp();
//  m_triggerAttr.nEdgeMode = CamObject::getInstance()->getTriggerEdge();
    m_triggerAttr.nDelayTm = CamObject::getInstance()->getDelayTime();
    m_triggerAttr.nFrames = 1;

    if (TUCCM_TRIGGER_STANDARD == m_triggerAttr.nTgrMode && TUCTE_EXPTM == m_triggerAttr.nExpMode)
    {
        m_triggerAttr.nFrames = ui->spbNum->value();
    }

    TUCAM_Cap_SetTrigger(hIdxTUCam, m_triggerAttr);

    if (isWaitting)
    {
        CamObject::getInstance()->startWaittingForFrames();
    }
}

void CamTrigger::totalFrameVisiable()
{
    int triggerMode = CamObject::getInstance()->getTriggerMode();
    int triggerExp  = CamObject::getInstance()->getTriggerExp();

    bool vsible = (TUCCM_TRIGGER_STANDARD == triggerMode &&  TUCTE_EXPTM == triggerExp);

    ui->labNum->setVisible(vsible);
    ui->spbNum->setVisible(vsible);
    ui->pbStop->setVisible(vsible);
}

void CamTrigger::on_spbNum_editingFinished()
{
    int value = ui->spbNum->value();
    if (value < 1)
        value = 1;

    if (value > 65535)
        value = 65535;

    ui->spbNum->setValue(value);
    setTrigger();
}

void CamTrigger::on_pbStop_clicked()
{
    setTrigger();
}
