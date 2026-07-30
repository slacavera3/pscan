#ifndef CAMIMAGESAVE_H
#define CAMIMAGESAVE_H

#include <QWidget>

namespace Ui {
class CamImageSave;
}

class CamImageSave : public QWidget
{
    Q_OBJECT

public:
    explicit CamImageSave(QWidget *parent = 0);
    ~CamImageSave();

    void initImageSaveValue();
    void bindConnections();

protected:
    void paintEvent(QPaintEvent *event);

private slots:
    void on_pbSave_clicked();

    void on_pbBrowse_clicked();

    void on_cbTiff_clicked(bool checked);

    void on_cbJpg_clicked(bool checked);

    void on_cbPng_clicked(bool checked);

    void on_cbBmp_clicked(bool checked);

    void on_cbRaw_clicked(bool checked);

    void slotSaving(int index);

    void slotSavingFinished();

private:
    Ui::CamImageSave *ui;

    int m_totalFrames;
    int m_countFormat;
    int m_savedFormat;
    int m_intervalTime;
    QString m_savePath;
    QString m_imageName;
};

#endif // CAMIMAGESAVE_H
